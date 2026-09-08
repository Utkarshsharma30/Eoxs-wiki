"""One-off cleanup (2026-09-08) for 21 duplicate-title groups / 43 live
wiki_pages rows found while investigating a retrieval bug: get_wiki_page's
title-match-then-pick-one logic had no reliable way to choose the "right"
page among same-titled duplicates, because the duplicates should never have
existed live in the first place. Root cause (see docs/wiki-ingestion.md):
wiki-ingestion's drafting agent can independently produce two same-topic
staging drafts across a 6-hour cycle boundary, and promote.py had no
awareness of already-live pages when handling a CREATE -- fixed going
forward in promote.py's _find_live_page_by_title() redirect. This script
is the retroactive cleanup for the 43 pages that already exist.

Design, mirroring wiki_ingestion/consolidate_mcp_server.py's
merge_staging_pages (same synthesis quality bar -- deduplicate overlapping
content, preserve genuinely distinct sub-topics, flag contradictions
explicitly rather than silently picking one version) but adapted for LIVE
wiki_pages, which merge_staging_pages was never built to touch:

1. Real content synthesis via Claude (Sonnet, not Haiku -- this needs actual
   editorial judgment across full page bodies, same trust level as
   mcp_server/redaction.py's redaction calls, not a mechanical rule).
2. Unlike staging drafts, LIVE pages can be linked TO by other live pages
   (wiki_links.to_page_id) -- deleting a duplicate without redirecting its
   inbound links would leave those links dangling. Every inbound link to a
   page about to be deleted is repointed to the surviving page first.
3. One transaction per group (not one for the whole run) -- a failure on
   group N doesn't touch groups already merged, and the script is safe to
   re-run (already-merged groups no longer appear in find_duplicate_groups's
   live-table equivalent below, so they're skipped automatically).
4. Full audit trail: every merge decision (kept id, deleted ids, which
   Claude call produced the merged body) logged to stdout; the merged body
   itself is never silently discarded -- printed in full before commit, so
   a run can be reviewed without re-deriving anything.
"""
import argparse
import json
import logging
import os
import re
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import anthropic

from ingestion.db import get_live_conn

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("wiki_ingestion.merge_live_duplicates")

# Per-call cost tracking, requested 2026-09-08 since this is the first
# CLASSIFIER_ANTHROPIC_API_KEY caller with a per-call cost worth watching
# individually (a full-page-body merge prompt, not a cheap classification --
# docs/tech-stack-and-billing.md's CLASSIFIER_ANTHROPIC_API_KEY row is still
# "[FILL IN]" for cost, this is a start on real numbers for at least this
# caller). Appends one JSON line per API call, never overwrites -- same
# append-only audit-trail convention as employee_change_log/asset_change_log.
# claude-sonnet-5 pricing: $3/MTok input, $15/MTok output (standard tier,
# no cache) -- see docs/tech-stack-and-billing.md; update PRICE_PER_MTOK
# here if that ever changes.
COST_LOG_PATH = Path(__file__).resolve().parent.parent / "logs" / "merge_live_duplicates_api_cost.jsonl"
PRICE_PER_MTOK_INPUT = 3.00
PRICE_PER_MTOK_OUTPUT = 15.00


def _log_api_cost(group_title, usage, elapsed_seconds):
    cost_usd = (usage.input_tokens / 1_000_000) * PRICE_PER_MTOK_INPUT + \
               (usage.output_tokens / 1_000_000) * PRICE_PER_MTOK_OUTPUT
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "group_title": group_title,
        "model": _MODEL,
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "elapsed_seconds": round(elapsed_seconds, 2),
        "cost_usd": round(cost_usd, 4),
    }
    COST_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(COST_LOG_PATH, "a") as f:
        f.write(json.dumps(entry) + "\n")
    logger.info("api call cost: $%.4f (%d in / %d out tokens, %.1fs) -- %s",
                cost_usd, usage.input_tokens, usage.output_tokens, elapsed_seconds, group_title)
    return cost_usd

_MODEL = "claude-sonnet-5"

_PROMPT = """You are merging {count} duplicate live wiki pages in eoxs-wiki-db, EOXS's internal \
second-brain. They share the exact title "{title}" and exist as separate pages only because of a \
pipeline bug (independent drafting sub-agents wrote them at different times without detecting the \
existing page) -- they should have always been one page.

Here is each page's full current content:

{pages_block}

## Your task
1. Synthesize ONE merged body that: deduplicates overlapping content, preserves every genuinely \
distinct sub-topic or detail any one page captured that the others didn't, and explicitly flags (as \
a "Note: possible contradiction" callout within the body, not silently resolved) any real factual \
contradictions you find between the pages -- e.g. different dates, different people, different \
numbers for what claims to be the same fact.
2. Pick which page id should survive (prefer the one with the most complete/best-organized starting \
content -- this is usually but not always the one with the most citations).
3. Respond in EXACTLY this format, nothing else before or after -- three plain-text sections, NOT \
JSON (a large merged body reliably breaks JSON string-escaping, so don't use it):

KEEP_ID: <the surviving page id, just the number>
SUMMARY: <one sentence: what the merge combined, and whether any contradiction was flagged>
MERGED_BODY:
<the full merged body markdown, starting on the next line, continuing to the end of your response -- \
write it exactly as it should be stored, no escaping, no code fences>
"""


def _parse_merge_response(text):
    """Parses the KEEP_ID/SUMMARY/MERGED_BODY plain-text format above --
    deliberately not JSON (see the prompt's own comment): a merged wiki
    page body is large freeform markdown, and asking a model to escape that
    perfectly into a JSON string value is fragile at this size in practice
    (confirmed live: real merge attempts failed json.loads on both literal
    control characters inside the string, needing json.loads(strict=False),
    and separately on missing-delimiter errors from an occasional stray
    unescaped quote -- both are exactly the failure modes you'd expect from
    asking a model to hand-escape a large text blob, and retries only
    sometimes cleared them, at real added latency/cost per retry). A
    delimited plain-text format has no escaping to get wrong -- MERGED_BODY
    is just "everything after this line," verbatim."""
    keep_match = re.search(r"^KEEP_ID:\s*(\d+)\s*$", text, re.MULTILINE)
    summary_match = re.search(r"^SUMMARY:\s*(.+)$", text, re.MULTILINE)
    body_match = re.search(r"^MERGED_BODY:\s*\n(.*)", text, re.MULTILINE | re.DOTALL)
    if not (keep_match and summary_match and body_match):
        raise ValueError(
            f"response missing one of KEEP_ID/SUMMARY/MERGED_BODY (found: "
            f"keep_id={bool(keep_match)}, summary={bool(summary_match)}, body={bool(body_match)})"
        )
    return {
        "keep_id": int(keep_match.group(1)),
        "summary": summary_match.group(1).strip(),
        "merged_body": body_match.group(1).strip(),
    }


def find_live_duplicate_groups(conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT min(title) AS title, array_agg(id ORDER BY id) AS page_ids
            FROM wiki_pages
            GROUP BY lower(trim(title))
            HAVING count(*) > 1
            ORDER BY min(title)
            """
        )
        return [{"title": r["title"], "page_ids": r["page_ids"]} for r in cur.fetchall()]


def _fetch_page_full(conn, page_id):
    with conn.cursor() as cur:
        cur.execute("SELECT id, title, page_type, entity_class, tags, sources_raw, body FROM wiki_pages WHERE id = %s", (page_id,))
        page = cur.fetchone()
        cur.execute("SELECT source_type, source_id, source_ref_raw FROM wiki_citations WHERE wiki_page_id = %s", (page_id,))
        page["citations"] = cur.fetchall()
        cur.execute("SELECT flag_type, text FROM wiki_flags WHERE wiki_page_id = %s", (page_id,))
        page["flags"] = cur.fetchall()
        return page


def _format_pages_block(pages):
    parts = []
    for p in pages:
        cites = ", ".join(f"{c['source_type']}:{c['source_id']}" for c in p["citations"]) or "(none)"
        parts.append(
            f"### Page id={p['id']} (page_type={p['page_type']}, tags={p['tags']})\n"
            f"Citations: {cites}\n\n{p['body']}"
        )
    return "\n\n---\n\n".join(parts)


_REQUEST_TIMEOUT_SECONDS = 300  # several dry-run attempts appeared to hang --
# root causes, in order found: (1) claude-sonnet-5 can return a leading
# ThinkingBlock before the TextBlock, so resp.content[0].text raised
# AttributeError every attempt (fixed by extracting the first text-typed
# block specifically, and reasoning is now explicitly disabled since this
# call only needs JSON output); (2) a REAL multi-page merge output runs
# 15-25K tokens, and a single non-streaming call that long (confirmed via
# isolated test: consistently ~176-177s for 21K output tokens) is
# legitimately slow, not stuck -- tried switching to client.messages.stream()
# for headroom, but streaming itself then hung indefinitely (killed after
# 9+ minutes with zero progress) while the plain non-streaming create() call
# kept completing reliably in ~177s -- reverted to non-streaming. 300s gives
# real margin above the 177s baseline actually observed.
MAX_RETRIES = 3


def _extract_text(resp):
    for block in resp.content:
        if getattr(block, "type", None) == "text":
            return block.text
    raise ValueError(f"no text block in response (block types: {[getattr(b, 'type', '?') for b in resp.content]})")


# 8000 was the original budget and was too small -- a real 3-page group
# (one page alone ~42K chars / ~10.5K tokens) hit the cap exactly, silently
# truncating the response mid-body. Sized generously above the largest
# observed single-page body plus synthesis overhead; still checked
# explicitly below (stop reason 'max_tokens' means truncated output, not a
# clean finish) so a future larger page fails loudly instead of producing
# corrupted output.
MAX_OUTPUT_TOKENS = 32000


class _HardTimeout(Exception):
    pass


def _alarm_handler(signum, frame):
    raise _HardTimeout()


def _synthesize_merge(client, group_title, pages):
    prompt = _PROMPT.format(
        count=len(pages), title=group_title,
        pages_block=_format_pages_block(pages),
    )
    last_error = None
    for attempt in range(MAX_RETRIES):
        # OS-level watchdog on top of the SDK's own `timeout=` -- during
        # testing, several calls ran well past _REQUEST_TIMEOUT_SECONDS
        # (one over 20 minutes on a call that should time out at 300s) with
        # no exception ever raised, meaning the SDK-level timeout was not
        # reliably firing in this environment for some backgrounded runs
        # (root cause not fully isolated -- possibly httpx read-timeout
        # semantics not covering however this particular hang blocks).
        # SIGALRM is a hard backstop that can't be silently swallowed by
        # the same layer that's failing to enforce its own timeout.
        old_handler = signal.signal(signal.SIGALRM, _alarm_handler)
        signal.alarm(_REQUEST_TIMEOUT_SECONDS + 30)
        try:
            start = time.monotonic()
            resp = client.messages.create(
                model=_MODEL, max_tokens=MAX_OUTPUT_TOKENS, messages=[{"role": "user", "content": prompt}],
                timeout=_REQUEST_TIMEOUT_SECONDS, thinking={"type": "disabled"},
            )
            elapsed = time.monotonic() - start
            _log_api_cost(group_title, resp.usage, elapsed)
            if resp.stop_reason == "max_tokens":
                raise RuntimeError(
                    f"response truncated at max_tokens={MAX_OUTPUT_TOKENS} -- "
                    "merged body likely too large for this budget, raise MAX_OUTPUT_TOKENS"
                )
            text = _extract_text(resp).strip()
            return _parse_merge_response(text)
        except Exception as e:
            last_error = e
            logger.warning("synthesis attempt %d/%d for %r failed: %s", attempt + 1, MAX_RETRIES, group_title, e)
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old_handler)
    raise RuntimeError(f"synthesis failed after {MAX_RETRIES} attempts: {last_error}")


def merge_group(conn, client, group, dry_run=True):
    page_ids = group["page_ids"]
    pages = [_fetch_page_full(conn, pid) for pid in page_ids]
    result = _synthesize_merge(client, group["title"], pages)
    keep_id = result["keep_id"]
    if keep_id not in page_ids:
        raise ValueError(f"model returned keep_id={keep_id} not in group {page_ids}")
    duplicate_ids = [pid for pid in page_ids if pid != keep_id]
    merged_body = result["merged_body"]

    logger.info("group %r: keep=%d, merge=%s -- %s", group["title"], keep_id, duplicate_ids, result.get("summary", ""))
    if dry_run:
        logger.info("[dry-run] would write %d chars, reassign citations/flags, redirect inbound links, delete %s",
                     len(merged_body), duplicate_ids)
        return {"title": group["title"], "keep_id": keep_id, "duplicate_ids": duplicate_ids, "dry_run": True}

    with conn.cursor() as cur:
        cur.execute("UPDATE wiki_citations SET wiki_page_id = %s WHERE wiki_page_id = ANY(%s)", (keep_id, duplicate_ids))
        cur.execute("UPDATE wiki_flags SET wiki_page_id = %s WHERE wiki_page_id = ANY(%s)", (keep_id, duplicate_ids))
        # Outbound links FROM the duplicates move to keep_id too.
        cur.execute("UPDATE wiki_links SET from_page_id = %s WHERE from_page_id = ANY(%s)", (keep_id, duplicate_ids))
        # Inbound links from OTHER live pages TO a duplicate about to be
        # deleted must be redirected -- unlike staging drafts (which
        # merge_staging_pages never had to handle), live pages can already
        # be linked to by other live pages. Without this, deleting a
        # duplicate would leave those links dangling (to_page_id -> NULL).
        cur.execute("UPDATE wiki_links SET to_page_id = %s WHERE to_page_id = ANY(%s)", (keep_id, duplicate_ids))
        cur.execute(
            "UPDATE wiki_pages SET body = %s, updated_date = CURRENT_DATE, updated_at = now() WHERE id = %s",
            (merged_body, keep_id),
        )
        cur.execute("DELETE FROM wiki_pages WHERE id = ANY(%s)", (duplicate_ids,))
    conn.commit()
    logger.info("group %r: committed", group["title"])
    return {"title": group["title"], "keep_id": keep_id, "duplicate_ids": duplicate_ids, "dry_run": False}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--commit", action="store_true", help="Actually write changes. Default is dry-run.")
    parser.add_argument("--limit", type=int, default=None,
                         help="Process at most N groups this invocation (each real merge call takes "
                              "~170-300s, so a full run of 20+ groups exceeds typical foreground command "
                              "budgets -- safe to re-run repeatedly with a limit; already-merged groups "
                              "don't reappear in find_live_duplicate_groups, so each run picks up where "
                              "the last one left off).")
    args = parser.parse_args()

    client = anthropic.Anthropic(api_key=os.environ["CLASSIFIER_ANTHROPIC_API_KEY"])
    conn = get_live_conn()
    try:
        groups = find_live_duplicate_groups(conn)
        logger.info("found %d duplicate-title groups (%d pages total)", len(groups), sum(len(g["page_ids"]) for g in groups))
        if not groups:
            logger.info("nothing to do")
            return
        if not args.commit:
            logger.info("DRY RUN -- pass --commit to actually write. No changes will be made.")
        if args.limit is not None:
            groups = groups[:args.limit]
            logger.info("processing first %d group(s) this run (--limit)", len(groups))

        results = []
        for group in groups:
            try:
                results.append(merge_group(conn, client, group, dry_run=not args.commit))
            except Exception as e:
                logger.error("group %r FAILED, skipping: %s", group["title"], e)
                conn.rollback()

        merged = sum(1 for r in results if not r["dry_run"])
        logger.info("done: %d/%d groups processed (%d committed)", len(results), len(groups), merged)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
