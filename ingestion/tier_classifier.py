"""Bulk access-tier classifier for Cruz's tiered access system. Reads real
content, decides one of three levels, fail-closed (more restrictive) on
any uncertainty, API error, or exhausted retries:

  tier1              Raj's own personal data ONLY (bank statements,
                      divorce, family/personal-life matters). No company
                      business, even if company-sensitive.
  tier2_confidential Company-confidential: salary/payroll/compensation/
                      incentive/bonus for ANY employee, investor relations
                      & fundraising, company financial statements/bank
                      data, vendor payment terms / sensitive pricing
                      contracts, legal/compliance matters (non-Raj-
                      personal), employee activity/performance/
                      productivity monitoring data (e.g. Cattr).
  tier2              General -- everything else, company-wide visible.

Second-generation version of this script: the first one only had 2 levels
(tier1 = Raj-personal + company-confidential conflated together) and only
classified the fraction of rows that survived a SQL pre-filter (domain/
keyword-based). Both are gone now -- every row in every table gets a real
LLM read, no pre-filter, no "structurally safe, skip" shortcut for
tickets/sales_orders/implementation_tasks -- the broader confidential
definition (legal/compliance, vendor contracts) could plausibly appear
anywhere, and the whole point of the redo is not repeating the "shallow
filter missed real matches" mistake found in the first version.

Writes are incremental (one UPDATE per row immediately after its verdict,
not batched at the end) -- the first version's first run was killed
mid-flight and lost 100% of its progress because it only wrote at the very
end.
"""
import asyncio
import logging
import os
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

Decide exactly one of three levels:

TIER1 -- Rajat "Raj" Jain's own PERSONAL data only:
- Raj's personal financial information (bank statements, personal investments, personal taxes)
- Divorce, family, or other personal/private life matters involving Raj
- Any other content that is personal to Raj rather than EOXS company business
Do NOT use TIER1 for company business, even if Raj is the sender/participant and even if it's
company-sensitive -- that belongs in TIER2_CONFIDENTIAL below.

TIER2_CONFIDENTIAL -- EOXS company-confidential business data:
- Salary, payroll, compensation, incentive, or bonus figures for ANY employee (including Raj's own)
- Investor relations and fundraising
- Company financial statements or bank/accounting data
- Vendor payment terms or contracts with sensitive pricing
- Legal or compliance matters (that are NOT Raj's personal legal matters)
- Employee activity, performance, or productivity monitoring data -- e.g. Cattr or similar
  tracking-tool output, individual performance metrics/scores, productivity reviews

TIER2 -- General, visible company-wide: ordinary business correspondence, client implementation/
support work, product/ops, sales orders, scheduling, recruiting (non-compensation details), and
other everyday professional content -- the default for anything not clearly TIER1 or
TIER2_CONFIDENTIAL.

When genuinely uncertain between two adjacent levels, prefer the more restrictive one (fail closed):
TIER2_CONFIDENTIAL over TIER2, or TIER1 over TIER2_CONFIDENTIAL if it's plausibly Raj's personal
matter rather than company business.

{context}

Answer with exactly one word: TIER1, TIER2_CONFIDENTIAL, or TIER2."""

_VALID = {"TIER1": "tier1", "TIER2_CONFIDENTIAL": "tier2_confidential", "TIER2": "tier2"}


def _fetch_pending(conn):
    """Every row in every tiered table -- full re-scan, not just the
    previous version's tier1 set (see module docstring)."""
    items = []
    with conn.cursor() as cur:
        cur.execute("SELECT id, subject FROM email_threads")
        thread_rows = cur.fetchall()
        for t in thread_rows:
            cur.execute(
                "SELECT body FROM email_messages WHERE thread_id = %s ORDER BY message_index LIMIT 1", (t["id"],)
            )
            body_row = cur.fetchone()
            body = (body_row["body"] if body_row else "") or ""
            context = f"Email subject: {t['subject']}\nBody (truncated):\n{body[:_MAX_CONTEXT_CHARS]}"
            items.append(("email_threads", t["id"], context))

        cur.execute("SELECT id, meeting_title, fireflies_summary, transcript_body FROM call_transcripts")
        for c in cur.fetchall():
            snippet = c["fireflies_summary"] or (c["transcript_body"] or "")[:_MAX_CONTEXT_CHARS]
            context = f"Call title: {c['meeting_title']}\nSummary/transcript (truncated):\n{snippet[:_MAX_CONTEXT_CHARS]}"
            items.append(("call_transcripts", c["id"], context))

        cur.execute("SELECT id, subject, description FROM tickets")
        for t in cur.fetchall():
            context = f"Support ticket subject: {t['subject']}\nDescription (truncated):\n{(t['description'] or '')[:_MAX_CONTEXT_CHARS]}"
            items.append(("tickets", t["id"], context))

        cur.execute("SELECT id, task_name, description FROM implementation_tasks")
        for t in cur.fetchall():
            context = f"Implementation task: {t['task_name']}\nDescription (truncated):\n{(t['description'] or '')[:_MAX_CONTEXT_CHARS]}"
            items.append(("implementation_tasks", t["id"], context))

        cur.execute(
            "SELECT id, order_number, client_raw, amount_total, currency, state_label, salesperson FROM sales_orders"
        )
        for s in cur.fetchall():
            context = (
                f"Sales order: {s['order_number']} | client: {s['client_raw']} | "
                f"amount: {s['amount_total']} {s['currency']} | state: {s['state_label']} | "
                f"salesperson: {s['salesperson']}"
            )
            items.append(("sales_orders", s["id"], context))
    return items


async def _classify_one(client, sem, context):
    async with sem:
        for attempt in range(MAX_RETRIES):
            try:
                resp = await client.messages.create(
                    model=_MODEL, max_tokens=8,
                    messages=[{"role": "user", "content": _PROMPT.format(context=context)}],
                )
                answer = resp.content[0].text.strip().upper()
                if "TIER2_CONFIDENTIAL" in answer:
                    return "tier2_confidential"
                if "TIER1" in answer:
                    return "tier1"
                return "tier2"
            except anthropic.RateLimitError:
                wait = min(2 ** attempt * 2, 60)
                logger.warning("rate limited, retrying in %ss (attempt %d/%d)", wait, attempt + 1, MAX_RETRIES)
                await asyncio.sleep(wait)
            except Exception as e:
                logger.warning("classification failed, defaulting to tier1: %s", e)
                return "tier1"
        logger.warning("exhausted retries, defaulting to tier1")
        return "tier1"


async def _run(conn, items):
    # Deliberately a separate key from ANTHROPIC_API_KEY (used by spam_filter.py and
    # everything else) -- the user wants this bulk job's usage/cost trackable on its
    # own in the Anthropic console, not blended into the main key's usage.
    client = anthropic.AsyncAnthropic(api_key=os.environ["CLASSIFIER_ANTHROPIC_API_KEY"])
    sem = asyncio.Semaphore(CONCURRENCY)
    completed = 0
    counts = {"tier1": 0, "tier2_confidential": 0, "tier2": 0}
    start = time.monotonic()

    async def worker(table, row_id, context):
        nonlocal completed
        verdict = await _classify_one(client, sem, context)
        # Synchronous write right after the (awaited, concurrent) LLM call
        # returns -- never interleaved with another await, so sharing one
        # psycopg2 connection across these coroutines is safe.
        with conn.cursor() as cur:
            cur.execute(f"UPDATE {table} SET access_tier = %s WHERE id = %s", (verdict, row_id))
        conn.commit()
        counts[verdict] += 1
        completed += 1
        if completed % 200 == 0 or completed == len(items):
            elapsed = time.monotonic() - start
            rate = completed / elapsed if elapsed else 0
            eta = (len(items) - completed) / rate if rate else 0
            logger.info("progress: %d/%d (%.1f/s, ~%.0fs remaining)", completed, len(items), rate, eta)

    await asyncio.gather(*(worker(table, row_id, ctx) for table, row_id, ctx in items))
    return counts


def main():
    conn = get_live_conn()
    try:
        items = _fetch_pending(conn)
        logger.info("pending classification: %d items", len(items))
        if not items:
            logger.info("nothing to classify")
            return

        counts = asyncio.run(_run(conn, items))
        logger.info(
            "totals: %d -> tier1, %d -> tier2_confidential, %d -> tier2",
            counts["tier1"], counts["tier2_confidential"], counts["tier2"],
        )

        with conn.cursor() as cur:
            for table in ("email_threads", "call_transcripts", "tickets", "implementation_tasks", "sales_orders"):
                cur.execute(f"SELECT access_tier, count(*) FROM {table} GROUP BY access_tier")
                for row in cur.fetchall():
                    logger.info("%s: %s = %d", table, row["access_tier"], row["count"])
    finally:
        conn.close()


if __name__ == "__main__":
    main()
