"""Fathom fetcher -- ports the REST fetch logic from the Render pipeline's
tools/fathom_to_obsidian/fathom_to_obsidian.py: cursor-paginated /meetings
(server-side created_after filter, unlike Fireflies/Zoho which need
client-side filtering) + per-recording /recordings/{id}/transcript,
the same noise filter as Fireflies (ingestion.call_relevance), and NO
client routing (all Fathom calls are unmatched/general, matching the old
pipeline). Final write step is Postgres instead of a markdown file + git
commit.

Usage: python -m ingestion.fathom_fetcher [--dry-run] [--limit N]
"""
import argparse
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests

from ingestion.db import dual_write, get_live_conn
from ingestion.state import sync_since, set_last_synced_at, now_utc
from ingestion.call_relevance import is_call_relevant
from ingestion.write_call import write_call, existing_call
from ingestion.inline_tier_classifier import classify_tier
from ingestion.retry import call_with_retry
from ingestion.tz import et_date

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("ingestion.fathom")

SOURCE = "fathom"
FATHOM_BASE = "https://api.fathom.ai/external/v1"
API_KEY_ENV = "RON_FATHOM_API_KEY"
DEFAULT_MAX_RESULTS = 2000
DEFAULT_SAFETY_OVERLAP_DAYS = 2
RATE_LIMIT_DELAY_SECONDS = 2.0  # mandatory courtesy pause, not just on error
FATHOM_MAX_RETRIES = 5
FATHOM_BASE_DELAY = 15
FATHOM_MAX_DELAY = 240


class _FathomRateLimited(Exception):
    def __init__(self, retry_after=None):
        self.retry_after = retry_after
        super().__init__("Fathom rate limited")


def _is_retryable(e):
    return isinstance(e, (_FathomRateLimited, requests.exceptions.ConnectionError, requests.exceptions.Timeout))


def _retry_after(e):
    return e.retry_after if isinstance(e, _FathomRateLimited) else None


def _do_fathom_get(api_key, path, params):
    resp = requests.get(f"{FATHOM_BASE}{path}", headers={"X-Api-Key": api_key}, params=params or {}, timeout=30)
    if resp.status_code == 429:
        header = resp.headers.get("Retry-After", "")
        wait = float(header) if header.isdigit() else None
        raise _FathomRateLimited(wait)
    resp.raise_for_status()
    return resp.json()


def fathom_get(api_key, path, params=None):
    return call_with_retry(
        lambda: _do_fathom_get(api_key, path, params),
        is_retryable=_is_retryable, retry_after_getter=_retry_after,
        max_retries=FATHOM_MAX_RETRIES, base_delay=FATHOM_BASE_DELAY, max_delay=FATHOM_MAX_DELAY,
    )


def fetch_all_meetings(api_key, created_after=None, limit_override=None):
    meetings = []
    cursor = None
    while True:
        params = {}
        if cursor:
            params["cursor"] = cursor
        if created_after:
            params["created_after"] = created_after
        data = fathom_get(api_key, "/meetings", params)
        batch = (data.get("items") if isinstance(data, dict) else None) or []
        meetings.extend(batch)
        if limit_override and len(meetings) >= limit_override:
            return meetings[:limit_override]
        cursor = data.get("next_cursor") if isinstance(data, dict) else None
        if not cursor or not batch:
            break
        time.sleep(RATE_LIMIT_DELAY_SECONDS)
    return meetings


def fetch_transcript(api_key, recording_id):
    try:
        result = fathom_get(api_key, f"/recordings/{recording_id}/transcript")
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            return result.get("transcript") or []
        return []
    except Exception as e:
        logger.warning("transcript fetch failed for %s: %s", recording_id, e)
        return []


def parse_dt(value):
    if not value:
        return None
    if isinstance(value, (int, float)):
        n = int(value)
        if n > 10_000_000_000:
            n //= 1000
        return datetime.fromtimestamp(n, tz=timezone.utc)
    s = str(value).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def meeting_start_dt(meeting):
    """Prefer recording_start_time (actual call start) over created_at (upload time)."""
    return parse_dt(meeting.get("recording_start_time") or meeting.get("created_at")) or now_utc()


def meeting_duration_seconds(meeting):
    """Fathom has no duration field -- derive it from start/end times."""
    start, end = meeting.get("recording_start_time"), meeting.get("recording_end_time")
    if start and end:
        s, e = parse_dt(start), parse_dt(end)
        if s and e:
            return max(0, int((e - s).total_seconds()))
    return 0


def fmt_duration(seconds):
    total = int(seconds or 0)
    m, s = divmod(total, 60)
    h, m = divmod(m, 60)
    return f"{h}h {m}m {s}s" if h else f"{m}m {s}s"


def normalize_attendees(raw):
    """Fathom's calendar_invitees: [{name, email, ...}, ...]."""
    seen, result = set(), []
    for entry in raw or []:
        label = entry.get("email") or entry.get("name") or "" if isinstance(entry, dict) else str(entry).strip()
        if label and label not in seen:
            seen.add(label)
            result.append(label)
    return sorted(result)


def build_segments(transcript):
    """Merges consecutive same-speaker transcript items into segments,
    mirroring the old pipeline's render_call() formatting."""
    segments = []
    current_speaker, buf = None, []

    def flush():
        if current_speaker and buf:
            segments.append({
                "segment_order": len(segments) + 1,
                "speaker": current_speaker,
                "text": " ".join(buf),
            })

    for item in transcript or []:
        speaker_raw = item.get("speaker") or {}
        speaker = (speaker_raw.get("display_name") if isinstance(speaker_raw, dict) else str(speaker_raw)).strip() or "Unknown"
        text = (item.get("text") or "").strip()
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


def should_include_by_date(meeting, since):
    if not since:
        return True
    return meeting_start_dt(meeting) >= since


def process_fathom(*, dry_run=False, limit=DEFAULT_MAX_RESULTS,
                    safety_overlap_days=DEFAULT_SAFETY_OVERLAP_DAYS, classify=True):
    api_key = os.environ[API_KEY_ENV]
    since = sync_since(SOURCE, safety_overlap_days)  # already overlap-adjusted
    created_after = since.strftime("%Y-%m-%dT%H:%M:%SZ") if since else None
    logger.info("source=%s since=%s created_after=%s limit=%d dry_run=%s", SOURCE, since, created_after, limit, dry_run)

    meetings = fetch_all_meetings(api_key, created_after=created_after, limit_override=limit)
    logger.info("source=%s candidate meetings=%d", SOURCE, len(meetings))

    counts = {"written": 0, "skipped_stale": 0, "skipped_old": 0, "skipped_noise": 0, "error": 0, "written_items": []}
    run_started_at = now_utc()

    for meeting in meetings:
        recording_id = str(meeting.get("recording_id") or meeting.get("id") or "")
        try:
            if not recording_id:
                counts["skipped_old"] += 1
                continue

            if not should_include_by_date(meeting, since):
                counts["skipped_old"] += 1
                continue

            conn = get_live_conn()
            try:
                already = existing_call(conn, SOURCE, recording_id)
            finally:
                conn.close()
            if already:
                counts["skipped_stale"] += 1
                continue

            transcript = fetch_transcript(api_key, recording_id)
            time.sleep(RATE_LIMIT_DELAY_SECONDS)

            excerpt = " ".join((item.get("text") or "").strip() for item in transcript[:40]).strip()
            duration_secs = meeting_duration_seconds(meeting)

            if classify and not is_call_relevant(
                meeting.get("title"), fmt_duration(duration_secs), None, [], [], excerpt,
            ):
                counts["skipped_noise"] += 1
                continue

            participants = normalize_attendees(meeting.get("calendar_invitees"))
            segments = build_segments(transcript)
            transcript_body = build_transcript_body(segments)
            call_dt = meeting_start_dt(meeting)

            if dry_run:
                logger.info("[dry-run] would write call %s: %r", recording_id, meeting.get("title"))
                counts["written"] += 1
                counts["written_items"].append(meeting.get("title") or "(untitled)")
                continue

            tier_context = (
                f"Call title: {meeting.get('title')}\n"
                f"Summary/transcript (truncated):\n{transcript_body}"
            )
            dual_write(
                write_call,
                source=SOURCE, external_id=recording_id,
                meeting_title=meeting.get("title") or "",
                call_date=et_date(call_dt),
                duration_seconds=duration_secs,
                duration_human=fmt_duration(duration_secs),
                host_email="", participants=participants,
                recording_url=meeting.get("share_url") or meeting.get("url") or "",
                fireflies_summary=None, key_topics=[], action_items=[],
                tags=["call", SOURCE], generated_at=now_utc(), client_id=None,
                transcript_body=transcript_body, segments=segments,
                access_tier=classify_tier(tier_context),
            )
            counts["written"] += 1
            counts["written_items"].append(meeting.get("title") or "(untitled)")

        except Exception as e:
            logger.error("call %s failed: %s", recording_id, e)
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

    process_fathom(dry_run=args.dry_run, limit=args.limit, classify=not args.no_classify)


if __name__ == "__main__":
    main()
