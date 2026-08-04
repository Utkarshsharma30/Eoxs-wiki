"""Bulk access-tier classifier for Cruz's tiered access system -- one-time
job over the historical backlog. Reads real content, decides tier1
(CEO-only) vs tier2 (general), fail-closed (stays tier1) on any
uncertainty, API error, or exhausted retries.

Same model/single-word-verdict pattern as spam_filter.py, but async +
concurrent (capped via CONCURRENCY) since this runs once over ~18,600
rows rather than inline per-item during live ingestion. Confirmed with
the user: concurrency=10, accept ~45-50 min wall-clock in exchange for a
large safety margin against rate limits, with real retry/backoff (not
just hoping) if one is hit anyway.

Going forward (not built here yet): live ingestion's write path should
set access_tier inline, the same way spam_filter.py already gates
writes -- this script is specifically for the pre-existing backlog that
predates the access-tier system.
"""
import asyncio
import logging
import os
import sys
import time

import anthropic

from ingestion.db import get_live_conn

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("ingestion.tier_classifier")

_MODEL = "claude-haiku-4-5-20251001"
_MAX_CONTEXT_CHARS = 2000
CONCURRENCY = 10
MAX_RETRIES = 5

_PROMPT = """You are classifying an item in Cruz, EOXS's internal knowledge base, for access control.

Decide TIER1 (CEO-only, hidden from every other employee) or TIER2 (general, visible company-wide).

Mark TIER1 ONLY if the content is clearly:
- Rajat "Raj" Jain's personal financial information (bank statements, personal investments, personal taxes)
- Divorce, family, or other personal/private life matters involving Raj
- Salary, payroll, compensation, incentive, or bonus figures for ANY employee (including Raj's own)
- Any other content that is personal to Raj rather than EOXS company business

Mark TIER2 for everything else, including ordinary business correspondence, client work, scheduling,
internal operations, and professional discussions -- even if Raj is a participant. When genuinely
uncertain, prefer TIER1 (fail closed -- this system defaults to restricting anything ambiguous rather
than risk exposing something sensitive).

{context}

Answer with exactly one word: TIER1 or TIER2."""


def _fetch_pending(conn):
    """Every row currently access_tier='tier1' across the four tables that
    need real classification (the ones already resolved via SQL --
    non-raj_gmail emails, known-domain calls, non-keyword tickets/tasks --
    are already tier2 and never reach this function)."""
    items = []
    with conn.cursor() as cur:
        cur.execute("SELECT id, subject FROM email_threads WHERE access_tier = 'tier1'")
        thread_rows = cur.fetchall()
        for t in thread_rows:
            cur.execute(
                "SELECT body FROM email_messages WHERE thread_id = %s ORDER BY message_index LIMIT 1", (t["id"],)
            )
            body_row = cur.fetchone()
            body = (body_row["body"] if body_row else "") or ""
            context = f"Email subject: {t['subject']}\nBody (truncated):\n{body[:_MAX_CONTEXT_CHARS]}"
            items.append(("email_threads", t["id"], context))

        cur.execute("SELECT id, meeting_title, fireflies_summary, transcript_body FROM call_transcripts WHERE access_tier = 'tier1'")
        for c in cur.fetchall():
            snippet = c["fireflies_summary"] or (c["transcript_body"] or "")[:_MAX_CONTEXT_CHARS]
            context = f"Call title: {c['meeting_title']}\nSummary/transcript (truncated):\n{snippet[:_MAX_CONTEXT_CHARS]}"
            items.append(("call_transcripts", c["id"], context))

        cur.execute("SELECT id, subject, description FROM tickets WHERE access_tier = 'tier1'")
        for t in cur.fetchall():
            context = f"Support ticket subject: {t['subject']}\nDescription (truncated):\n{(t['description'] or '')[:_MAX_CONTEXT_CHARS]}"
            items.append(("tickets", t["id"], context))

        cur.execute("SELECT id, task_name, description FROM implementation_tasks WHERE access_tier = 'tier1'")
        for t in cur.fetchall():
            context = f"Implementation task: {t['task_name']}\nDescription (truncated):\n{(t['description'] or '')[:_MAX_CONTEXT_CHARS]}"
            items.append(("implementation_tasks", t["id"], context))
    return items


async def _classify_one(client, sem, context):
    async with sem:
        for attempt in range(MAX_RETRIES):
            try:
                resp = await client.messages.create(
                    model=_MODEL, max_tokens=5,
                    messages=[{"role": "user", "content": _PROMPT.format(context=context)}],
                )
                answer = resp.content[0].text.strip().upper()
                return "tier2" if "TIER2" in answer else "tier1"
            except anthropic.RateLimitError:
                wait = min(2 ** attempt * 2, 60)
                logger.warning("rate limited, retrying in %ss (attempt %d/%d)", wait, attempt + 1, MAX_RETRIES)
                await asyncio.sleep(wait)
            except Exception as e:
                logger.warning("classification failed, defaulting to tier1: %s", e)
                return "tier1"
        logger.warning("exhausted retries, defaulting to tier1")
        return "tier1"


async def _run(items):
    client = anthropic.AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    sem = asyncio.Semaphore(CONCURRENCY)
    results = [None] * len(items)
    completed = 0
    start = time.monotonic()

    async def worker(i, context):
        nonlocal completed
        results[i] = await _classify_one(client, sem, context)
        completed += 1
        if completed % 200 == 0 or completed == len(items):
            elapsed = time.monotonic() - start
            rate = completed / elapsed if elapsed else 0
            eta = (len(items) - completed) / rate if rate else 0
            logger.info("progress: %d/%d (%.1f/s, ~%.0fs remaining)", completed, len(items), rate, eta)

    await asyncio.gather(*(worker(i, ctx) for i, (_, _, ctx) in enumerate(items)))
    return results


def main():
    conn = get_live_conn()
    try:
        items = _fetch_pending(conn)
        logger.info("pending classification: %d items", len(items))
        if not items:
            logger.info("nothing to classify")
            return

        results = asyncio.run(_run(items))

        by_table = {}
        for (table, row_id, _), verdict in zip(items, results):
            by_table.setdefault(table, {"tier1": [], "tier2": []})[verdict].append(row_id)

        with conn.cursor() as cur:
            for table, verdicts in by_table.items():
                for tier, ids in verdicts.items():
                    if ids:
                        cur.execute(f"UPDATE {table} SET access_tier = %s WHERE id = ANY(%s)", (tier, ids))
        conn.commit()

        for table, verdicts in by_table.items():
            logger.info("%s: %d -> tier1, %d -> tier2", table, len(verdicts["tier1"]), len(verdicts["tier2"]))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
