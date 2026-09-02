"""One-time re-classification pass triggered by the tier2_confidential_hr
split (schema/035_tier2_confidential_hr.sql). Two things at once, in a
single LLM read per row so cost isn't doubled:

  1. Carve HR-only content (payroll/salary/compensation/incentive/bonus,
     onboarding/offboarding, disciplinary action, sensitive credentials)
     OUT of tier2_confidential and tier1/tier2 into the new
     tier2_confidential_hr level, so it becomes structurally invisible to
     the `general` (internal team) identity -- previously the only thing
     keeping this content out of general's responses was the query-time
     redaction layer stripping it after the row was already visible.
  2. Health-audit every existing tier2_confidential row system-wide for
     OTHER misclassifications while already doing a real content read of
     each one -- found live during this same investigation: an asset
     (eoxs-client-implementation-go-live-sop) landed in tier2_confidential
     purely because its own front-matter carries an "Internal and
     Confidential" label, not because of any actual confidential content.
     Classify by SUBSTANCE, never by a document's self-declared label.

Scope, per explicit instruction:
  - assets: every row currently tier2_confidential (2 rows) -- includes the
    SOP false-positive fix and the salary register's HR reclassification.
  - wiki_pages: every row currently tier2_confidential (1,130 rows).
  - email_threads: every row currently tier2_confidential, ALL FIVE
    accounts, not just isha_zoho (7,971 rows) -- Isha's mailbox is the
    named HR-relevant source, but the same false-positive risk (and the
    same possibility of HR content elsewhere -- e.g. Raj discussing a
    termination) applies system-wide, matching the "full re-scan" choice
    made for every other table here.
  - call_transcripts, implementation_tasks, tickets, sales_orders: every
    row currently tier2_confidential, same full-audit scope.
  - repo_docs: excluded -- every row is hardcoded tier1 (never
    tier2_confidential), by deliberate design (CLAUDE.md), nothing to
    re-scan.
  - Rows currently tier1/tier2 are NOT re-scanned -- the instruction was to
    look at company-confidential content for an HR carve-out, and re-
    scanning literally everything (17k+ email rows alone) for a second
    time this session is a materially different, much larger job than what
    was asked for.

Verdict model -- 4 possible outcomes per row, not a plain tier label,
because upgrades and downgrades are handled with different trust levels:
  KEEP           -- no change (row is correctly tier2_confidential today).
  UPGRADE_HR     -- move to tier2_confidential_hr. Applied immediately,
                    same as the existing bulk tier_classifier.py's
                    unattended writes -- moving content INTO a MORE
                    restrictive tier is low-risk.
  DOWNGRADE_TIER1 -- content reads as Raj-personal, not company business.
  DOWNGRADE_TIER2 -- content reads as ordinary, not confidential at all
                    (the SOP-shaped false positive).
Both DOWNGRADE_* verdicts are NEVER auto-applied -- they widen who can see
the row, so they're written only to reclassify_hr_tier_review.csv for a
human to review and apply by hand. See _apply_downgrade.py-shaped note at
the bottom of main() -- there isn't one; approval is a separate manual step
by design.

Same CLASSIFIER_ANTHROPIC_API_KEY, same Haiku model, same incremental-write/
fail-closed conventions as ingestion/tier_classifier.py.
"""
import asyncio
import csv
import logging
import os
import time

import anthropic

from ingestion.db import get_live_conn

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("ingestion.reclassify_hr_tier")

_MODEL = "claude-haiku-4-5-20251001"
_MAX_CONTEXT_CHARS = 3000
CONCURRENCY = 10
MAX_RETRIES = 5

REVIEW_CSV = "reclassify_hr_tier_review.csv"

_PROMPT = """You are re-classifying an item already tagged "tier2_confidential" in Cruz, \
EOXS's internal knowledge base, for access control. Judge STRICTLY by the item's actual \
content -- never by a self-declared label like "Confidential" or "Internal and \
Confidential" appearing in the document's own text or front-matter. A document calling \
itself confidential is not evidence of anything; only the real substance counts.

Decide exactly one of four outcomes:

TIER2_CONFIDENTIAL_HR -- Employee-facing HR/financial content:
- Payroll, salary, compensation, incentive, or bonus figures or discussion for ANY employee
- Onboarding or offboarding paperwork/process (offer letters, exit process, final settlement)
- Disciplinary action: penalisation, suspension, termination-for-cause detail
- Sensitive credential material: account/system passwords, access-recovery secrets, login details
Use this ONLY when the item's actual content is genuinely about one of these -- not merely a
document that happens to be HR-adjacent (e.g. an ordinary onboarding SOP describing the general
process with no real person's compensation/discipline/credential detail is NOT this category).

TIER2_CONFIDENTIAL -- Other EOXS company-confidential business data that stays at this level:
- Investor relations and fundraising
- Company financial statements or bank/accounting data
- Vendor payment terms or contracts with sensitive pricing
- Legal or compliance matters (that are NOT Raj's personal legal matters)
- Employee activity, performance, or productivity monitoring data (e.g. Cattr, individual
  performance metrics/scores, productivity reviews)

TIER1 -- Rajat "Raj" Jain's own PERSONAL data only (not company business, even if
company-sensitive): personal financial information, divorce/family/other personal-life matters.

TIER2 -- General, visible company-wide: ordinary business correspondence, generic SOPs/process
docs with no real confidential detail, client implementation/support work, product/ops, sales
orders, scheduling, recruiting (non-compensation details). Use this when the item does NOT
actually contain confidential content, regardless of any "Confidential" label on the document
itself.

When genuinely uncertain between two adjacent levels, prefer the more restrictive one (fail
closed): TIER2_CONFIDENTIAL_HR or TIER2_CONFIDENTIAL over TIER2.

{context}

Answer with exactly one word: TIER2_CONFIDENTIAL_HR, TIER2_CONFIDENTIAL, TIER1, or TIER2."""

_VALID = {
    "TIER2_CONFIDENTIAL_HR": "tier2_confidential_hr",
    "TIER2_CONFIDENTIAL": "tier2_confidential",
    "TIER1": "tier1",
    "TIER2": "tier2",
}


def _fetch_pending(conn):
    """Every row currently tier2_confidential, across every tiered table
    except repo_docs (hardcoded tier1, nothing to re-scan there)."""
    items = []
    with conn.cursor() as cur:
        cur.execute("SELECT id, slug, title, body FROM assets WHERE access_tier = 'tier2_confidential'")
        for a in cur.fetchall():
            context = f"Asset title: {a['title']}\nBody (truncated):\n{(a['body'] or '')[:_MAX_CONTEXT_CHARS]}"
            items.append(("assets", a["id"], a["title"], context))

        cur.execute("SELECT id, title, body FROM wiki_pages WHERE access_tier = 'tier2_confidential'")
        for w in cur.fetchall():
            context = f"Wiki page title: {w['title']}\nBody (truncated):\n{(w['body'] or '')[:_MAX_CONTEXT_CHARS]}"
            items.append(("wiki_pages", w["id"], w["title"], context))

        cur.execute(
            "SELECT id, subject, source_account FROM email_threads WHERE access_tier = 'tier2_confidential'"
        )
        thread_rows = cur.fetchall()
        for t in thread_rows:
            cur.execute(
                "SELECT body FROM email_messages WHERE thread_id = %s ORDER BY message_index LIMIT 3", (t["id"],)
            )
            bodies = [r["body"] or "" for r in cur.fetchall()]
            snippet = "\n---\n".join(bodies)[:_MAX_CONTEXT_CHARS]
            context = f"Email account: {t['source_account']}\nSubject: {t['subject']}\nBody (truncated):\n{snippet}"
            label = f"{t['source_account']}: {t['subject']}"
            items.append(("email_threads", t["id"], label, context))

        cur.execute(
            "SELECT id, meeting_title, fireflies_summary, transcript_body FROM call_transcripts "
            "WHERE access_tier = 'tier2_confidential'"
        )
        for c in cur.fetchall():
            snippet = c["fireflies_summary"] or (c["transcript_body"] or "")[:_MAX_CONTEXT_CHARS]
            context = f"Call title: {c['meeting_title']}\nSummary/transcript (truncated):\n{snippet[:_MAX_CONTEXT_CHARS]}"
            items.append(("call_transcripts", c["id"], c["meeting_title"] or f"call {c['id']}", context))

        cur.execute(
            "SELECT id, task_name, description FROM implementation_tasks WHERE access_tier = 'tier2_confidential'"
        )
        for t in cur.fetchall():
            context = f"Implementation task: {t['task_name']}\nDescription (truncated):\n{(t['description'] or '')[:_MAX_CONTEXT_CHARS]}"
            items.append(("implementation_tasks", t["id"], t["task_name"], context))

        cur.execute("SELECT id, subject, description FROM tickets WHERE access_tier = 'tier2_confidential'")
        for t in cur.fetchall():
            context = f"Support ticket subject: {t['subject']}\nDescription (truncated):\n{(t['description'] or '')[:_MAX_CONTEXT_CHARS]}"
            items.append(("tickets", t["id"], t["subject"], context))

        cur.execute(
            "SELECT id, order_number, client_raw, amount_total, currency, state_label, salesperson "
            "FROM sales_orders WHERE access_tier = 'tier2_confidential'"
        )
        for s in cur.fetchall():
            context = (
                f"Sales order: {s['order_number']} | client: {s['client_raw']} | "
                f"amount: {s['amount_total']} {s['currency']} | state: {s['state_label']} | "
                f"salesperson: {s['salesperson']}"
            )
            items.append(("sales_orders", s["id"], s["order_number"], context))
    return items


async def _classify_one(client, sem, context):
    async with sem:
        for attempt in range(MAX_RETRIES):
            try:
                resp = await client.messages.create(
                    model=_MODEL, max_tokens=12,
                    messages=[{"role": "user", "content": _PROMPT.format(context=context)}],
                )
                answer = resp.content[0].text.strip().upper()
                for key in ("TIER2_CONFIDENTIAL_HR", "TIER2_CONFIDENTIAL", "TIER1", "TIER2"):
                    if key in answer:
                        return _VALID[key]
                return "tier2_confidential"  # unparseable -- fail closed at current level
            except anthropic.RateLimitError:
                wait = min(2 ** attempt * 2, 60)
                logger.warning("rate limited, retrying in %ss (attempt %d/%d)", wait, attempt + 1, MAX_RETRIES)
                await asyncio.sleep(wait)
            except Exception as e:
                logger.warning("classification failed, keeping current tier2_confidential: %s", e)
                return "tier2_confidential"
        logger.warning("exhausted retries, keeping current tier2_confidential")
        return "tier2_confidential"


async def _run(conn, items, review_rows):
    client = anthropic.AsyncAnthropic(api_key=os.environ["CLASSIFIER_ANTHROPIC_API_KEY"])
    sem = asyncio.Semaphore(CONCURRENCY)
    completed = 0
    counts = {"kept": 0, "upgraded_hr": 0, "downgrade_flagged": 0}
    start = time.monotonic()

    async def worker(table, row_id, label, context):
        nonlocal completed
        verdict = await _classify_one(client, sem, context)
        if verdict == "tier2_confidential":
            counts["kept"] += 1
        elif verdict == "tier2_confidential_hr":
            with conn.cursor() as cur:
                cur.execute(f"UPDATE {table} SET access_tier = %s WHERE id = %s", (verdict, row_id))
            conn.commit()
            counts["upgraded_hr"] += 1
            logger.info("UPGRADED to tier2_confidential_hr: %s#%s (%s)", table, row_id, label)
        else:
            # tier1 or tier2 -- a downgrade from tier2_confidential, widens
            # visibility. Never auto-applied; logged for manual review only.
            review_rows.append({"table": table, "id": row_id, "label": label, "recommended_tier": verdict})
            counts["downgrade_flagged"] += 1
            logger.info("DOWNGRADE CANDIDATE (not applied): %s#%s (%s) -> %s", table, row_id, label, verdict)
        completed += 1
        if completed % 200 == 0 or completed == len(items):
            elapsed = time.monotonic() - start
            rate = completed / elapsed if elapsed else 0
            eta = (len(items) - completed) / rate if rate else 0
            logger.info("progress: %d/%d (%.1f/s, ~%.0fs remaining)", completed, len(items), rate, eta)

    await asyncio.gather(*(worker(table, row_id, label, ctx) for table, row_id, label, ctx in items))
    return counts


def main():
    conn = get_live_conn()
    review_rows = []
    try:
        items = _fetch_pending(conn)
        logger.info("re-scanning %d tier2_confidential rows", len(items))
        if not items:
            logger.info("nothing to re-scan")
            return

        counts = asyncio.run(_run(conn, items, review_rows))
        logger.info(
            "totals: %d kept at tier2_confidential, %d upgraded to tier2_confidential_hr, "
            "%d downgrade candidates flagged for manual review",
            counts["kept"], counts["upgraded_hr"], counts["downgrade_flagged"],
        )

        if review_rows:
            with open(REVIEW_CSV, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=["table", "id", "label", "recommended_tier"])
                writer.writeheader()
                writer.writerows(review_rows)
            logger.info("%d downgrade candidates written to %s for manual review", len(review_rows), REVIEW_CSV)

        with conn.cursor() as cur:
            for table in (
                "assets", "wiki_pages", "email_threads", "call_transcripts",
                "implementation_tasks", "tickets", "sales_orders",
            ):
                cur.execute(f"SELECT access_tier, count(*) FROM {table} GROUP BY access_tier")
                for row in cur.fetchall():
                    logger.info("%s: %s = %d", table, row["access_tier"], row["count"])
    finally:
        conn.close()


if __name__ == "__main__":
    main()
