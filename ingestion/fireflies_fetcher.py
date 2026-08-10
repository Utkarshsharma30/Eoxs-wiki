"""Fireflies fetcher -- ports the GraphQL fetch logic from the Render
pipeline's tools/fireflies_to_obsidian/fireflies_to_obsidian.py: fetches
ALL call stubs (no server-side date filter -- Fireflies doesn't offer a
reliable one, same rationale as Zoho's client-side date filtering),
client-side date-filters against the sync cursor, fetches full transcript
detail per candidate, applies the noise filter (ingestion.call_relevance),
routes to a client via ingestion.routing, and writes via
ingestion.write_call. Final write step is Postgres instead of a markdown
file + git commit.

No email-style spam filter (ingestion.spam_filter.is_eoxs_relevant is
Gmail/Zoho-only) -- see ingestion/call_relevance.py's docstring for why
the noise filter is still applied here.

Usage: python -m ingestion.fireflies_fetcher [--dry-run] [--limit N]
"""
import argparse
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests

from ingestion.db import dual_write, get_live_conn
from ingestion.state import sync_since, set_last_synced_at, now_utc
from ingestion.call_relevance import is_call_relevant
from ingestion.routing import load_client_index, classify_client
from ingestion.write_call import write_call, existing_call
from ingestion.inline_tier_classifier import classify_tier
from ingestion.retry import call_with_retry
from ingestion.tz import et_date

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("ingestion.fireflies")

SOURCE = "fireflies"
FIREFLIES_ENDPOINT = "https://api.fireflies.ai/graphql"
DEFAULT_MAX_RESULTS = 2000
DEFAULT_SAFETY_OVERLAP_DAYS = 2
LIST_PAGE_SIZE = 50
DETAIL_PAUSE_SECONDS = 0.3

_LIST_QUERY = """
query Transcripts($limit: Int!, $skip: Int!) {
  transcripts(limit: $limit, skip: $skip) {
    id title date duration host_email participants
    summary { overview keywords action_items }
    transcript_url video_url
  }
}
"""

_DETAIL_QUERY = """
query Transcript($id: String!) {
  transcript(id: $id) {
    id title date duration host_email participants
    summary { overview keywords action_items }
    sentences { index speaker_id speaker_name text raw_text start_time }
    transcript_url video_url
  }
}
"""


class _FirefliesRateLimited(Exception):
    def __init__(self, retry_after=None):
        self.retry_after = retry_after
        super().__init__("Fireflies rate limited")


def _retry_after_from_error_text(error_text):
    match = re.search(r"'retryAfter':\s*(\d+)", error_text)
    if not match:
        return None
    retry_at_ms = int(match.group(1))
    wait = retry_at_ms / 1000.0 - datetime.now(timezone.utc).timestamp()
    return min(wait + 2, 300) if wait > 0 else None


def _is_retryable(e):
    return isinstance(e, (_FirefliesRateLimited, requests.exceptions.ConnectionError, requests.exceptions.Timeout))


def _retry_after(e):
    return e.retry_after if isinstance(e, _FirefliesRateLimited) else None


def _do_gql_request(api_key, query, variables):
    resp = requests.post(
        FIREFLIES_ENDPOINT,
        json={"query": query, "variables": variables or {}},
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        timeout=30,
    )
    if resp.status_code == 429:
        header = resp.headers.get("Retry-After")
        raise _FirefliesRateLimited(float(header) if header else None)
    resp.raise_for_status()
    body = resp.json()
    if "errors" in body:
        errors = body["errors"]
        if any(isinstance(e, dict) and e.get("code") == "too_many_requests" for e in errors):
            raise _FirefliesRateLimited(_retry_after_from_error_text(str(errors)))
        raise RuntimeError(f"Fireflies GraphQL error: {errors}")
    return body.get("data", {})


def gql(api_key, query, variables=None):
    return call_with_retry(
        lambda: _do_gql_request(api_key, query, variables),
        is_retryable=_is_retryable, retry_after_getter=_retry_after,
    )


def fetch_all_stubs(api_key, limit_override=None):
    stubs, skip = [], 0
    while True:
        data = gql(api_key, _LIST_QUERY, {"limit": LIST_PAGE_SIZE, "skip": skip})
        batch = data.get("transcripts") or []
        stubs.extend(batch)
        if limit_override and len(stubs) >= limit_override:
            return stubs[:limit_override]
        if len(batch) < LIST_PAGE_SIZE:
            break
        skip += LIST_PAGE_SIZE
    return stubs


def fetch_detail(api_key, transcript_id):
    data = gql(api_key, _DETAIL_QUERY, {"id": transcript_id})
    return (data or {}).get("transcript") or {}


def normalize_participants(raw):
    seen, result = set(), []
    for entry in raw or []:
        for p in str(entry).split(","):
            p = p.strip()
            if p and p not in seen:
                seen.add(p)
                result.append(p)
    return sorted(result)


def clean_sentence(text):
    text = re.sub(r"\b(um|uh),?\s*", "", text or "", flags=re.IGNORECASE)
    return re.sub(r" {2,}", " ", text).strip()


def ts_to_dt(ts):
    if ts is None:
        return None
    n = int(ts)
    if n > 10_000_000_000:
        n //= 1000
    return datetime.fromtimestamp(n, tz=timezone.utc)


def duration_to_seconds(minutes_float):
    return int(round(float(minutes_float or 0) * 60))


def fmt_duration(minutes_float):
    total = duration_to_seconds(minutes_float)
    m, s = divmod(total, 60)
    h, m = divmod(m, 60)
    return f"{h}h {m}m {s}s" if h else f"{m}m {s}s"


def action_items_list(raw):
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(x) for x in raw if x]
    return [ln.strip("- ").strip() for ln in str(raw).splitlines() if ln.strip()]


def build_segments(sentences):
    """Merges consecutive same-speaker sentences into segments, mirroring
    the old pipeline's render_call() transcript formatting."""
    segments = []
    current_speaker, buf = None, []

    def flush():
        if current_speaker and buf:
            segments.append({
                "segment_order": len(segments) + 1,
                "speaker": current_speaker,
                "text": " ".join(buf),
            })

    for sent in sentences or []:
        speaker = (sent.get("speaker_name") or "Unknown").strip()
        text = clean_sentence(sent.get("text") or sent.get("raw_text") or "")
        if not text:
            continue
        if speaker != current_speaker:
            flush()
            current_speaker, buf = speaker, [text]
        else:
            buf.append(text)
    flush()
    return segments


def build_transcript_body(segments):
    return "\n\n".join(f"**{seg['speaker']}:** {seg['text']}" for seg in segments)


def process_fireflies(*, dry_run=False, limit=DEFAULT_MAX_RESULTS,
                       safety_overlap_days=DEFAULT_SAFETY_OVERLAP_DAYS, classify=True):
    api_key = os.environ["FIREFLIES_API_KEY"]
    since = sync_since(SOURCE, safety_overlap_days)
    logger.info("source=%s since=%s limit=%d dry_run=%s", SOURCE, since, limit, dry_run)

    stubs = fetch_all_stubs(api_key, limit)
    logger.info("source=%s candidate stubs=%d", SOURCE, len(stubs))

    client_index = load_client_index()
    counts = {"written": 0, "skipped_stale": 0, "skipped_old": 0, "skipped_noise": 0, "error": 0, "written_items": []}
    run_started_at = now_utc()

    for i, stub in enumerate(stubs, 1):
        try:
            external_id = stub["id"]
            stub_date = ts_to_dt(stub.get("date"))
            if since and stub_date and stub_date < since:
                counts["skipped_old"] += 1
                continue

            conn = get_live_conn()
            try:
                already = existing_call(conn, SOURCE, external_id)
            finally:
                conn.close()
            if already:
                counts["skipped_stale"] += 1
                continue

            detail = fetch_detail(api_key, external_id)
            time.sleep(DETAIL_PAUSE_SECONDS)
            if not detail:
                counts["skipped_stale"] += 1
                continue

            summary = detail.get("summary") or {}
            overview = (summary.get("overview") or "").strip()
            key_topics = summary.get("keywords") or []
            if not isinstance(key_topics, list):
                key_topics = [key_topics]
            action_items = action_items_list(summary.get("action_items"))
            sentences = detail.get("sentences") or []
            excerpt = " ".join(
                clean_sentence(s.get("text") or s.get("raw_text") or "") for s in sentences[:40]
            ).strip()

            if classify and not is_call_relevant(
                detail.get("title"), fmt_duration(detail.get("duration")),
                overview, key_topics, action_items, excerpt,
            ):
                counts["skipped_noise"] += 1
                continue

            participants = normalize_participants(detail.get("participants"))
            client_id = classify_client(client_index, participants)
            segments = build_segments(sentences)
            transcript_body = build_transcript_body(segments)
            call_dt = ts_to_dt(detail.get("date"))

            if dry_run:
                logger.info("[dry-run] would write call %s: %r (client_id=%s)",
                            external_id, detail.get("title"), client_id)
                counts["written"] += 1
                counts["written_items"].append(detail.get("title") or "(untitled)")
                continue

            tier_context = (
                f"Call title: {detail.get('title')}\n"
                f"Summary/transcript (truncated):\n{overview or transcript_body}"
            )
            dual_write(
                write_call,
                source=SOURCE, external_id=external_id,
                meeting_title=detail.get("title") or "",
                call_date=et_date(call_dt),
                duration_seconds=duration_to_seconds(detail.get("duration")),
                duration_human=fmt_duration(detail.get("duration")),
                host_email=detail.get("host_email") or "",
                participants=participants,
                recording_url=detail.get("transcript_url") or detail.get("video_url") or "",
                fireflies_summary=overview, key_topics=key_topics, action_items=action_items,
                tags=["call", SOURCE], generated_at=now_utc(), client_id=client_id,
                transcript_body=transcript_body, segments=segments,
                access_tier=classify_tier(tier_context),
            )
            counts["written"] += 1
            counts["written_items"].append(detail.get("title") or "(untitled)")

        except Exception as e:
            logger.error("call %s failed: %s", stub.get("id"), e)
            counts["error"] += 1

    if not dry_run:
        set_last_synced_at(SOURCE, run_started_at)

    logger.info("source=%s done: %s", SOURCE, counts)
    return counts


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=DEFAULT_MAX_RESULTS)
    parser.add_argument("--no-classify", action="store_true")
    args = parser.parse_args()

    process_fireflies(dry_run=args.dry_run, limit=args.limit, classify=not args.no_classify)


if __name__ == "__main__":
    main()
