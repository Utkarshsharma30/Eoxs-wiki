"""Access-tier classifier for the 1,046 historically-live wiki pages (all
imported by the old vault loader, none through the new DB-native
promote.py pipeline -- see promote.py's own tier computation for pages
promoted going forward, which is cheap and reliable there because
citations are properly resolved at write time).

Second-generation version: the 3-level scheme (tier1 = Raj-personal only,
tier2_confidential = company-confidential, tier2 = general) replaces the
original 2-level one, and "confidential" is broader than the old tier1's
"salary/payroll" slice -- so this re-classifies every page from scratch,
not just the previous tier1 set.

Why this can't just reuse promote.py's citation-MAX approach: of these
pages' 1,122 citation rows, only 66 resolve to a real raw source row, 345
point at ANOTHER wiki page (indirection, not evidence -- see session
discussion), and 711 are permanently unresolved free-text slugs from the
old markdown frontmatter parser. Trusting that graph would leave the
large majority of pages unclassifiable. So instead: classify each page's
OWN body content directly (same pattern as ingestion/tier_classifier.py's
raw-data job, same CLASSIFIER_ANTHROPIC_API_KEY), then use whatever
citations DO resolve as a cheap, pure-SQL safety upgrade on top -- never
a downgrade, only ever a move to a MORE restrictive level.

Two backstop passes after the LLM pass, in order, both rank-based
(tier1=3 > tier2_confidential=2 > tier2=1) so they only ever tighten,
never loosen, a page's tier:
  1. Any page upgrades to the MOST restrictive level among its resolved
     citations to a real raw row (email_thread/call_transcript/ticket by
     id, implementation_task by odoo_task_id -- same fail-closed-on-
     collision join as promote.py), if that's stricter than its own
     content verdict.
  2. Any page that cites ANOTHER wiki page (source_type='wiki_page', all
     345 of these are resolved) upgrades to that cited page's level if
     stricter -- possibly only *because* of pass 1, or a previous round
     of this same pass on a different page. Repeated to a fixed point
     since this is a real graph (A cites B cites C, C tightened by
     content or pass 1, should propagate to B and then A). Monotonic and
     bounded (at most 1,046 pages can tighten a bounded number of times),
     so this always terminates.

Writes are incremental (one UPDATE per page immediately after its
verdict, not batched at the end) -- ingestion/tier_classifier.py's first
run was killed mid-flight and lost 100% of its progress because it only
wrote at the very end; not repeating that here.
"""
import asyncio
import logging
import os
import time

import anthropic

from ingestion.db import get_live_conn

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("wiki_ingestion.tier_classifier")

_MODEL = "claude-haiku-4-5-20251001"
_MAX_CONTEXT_CHARS = 6000  # wiki pages are synthesized analyses, denser than a single raw message -- more context than the raw-data job
CONCURRENCY = 10
MAX_RETRIES = 5

_PROMPT = """You are classifying a synthesized wiki page in Cruz, EOXS's internal knowledge base, for access control.

Decide exactly one of three levels:

TIER1 -- Rajat "Raj" Jain's own PERSONAL data only:
- Raj's personal financial information (bank statements, personal investments, personal taxes)
- Divorce, family, or other personal/private life matters involving Raj
- Any other content that is personal to Raj rather than EOXS company business
Do NOT use TIER1 for company business, even if Raj is a participant/subject and even if it's
company-sensitive -- that belongs in TIER2_CONFIDENTIAL below.

TIER2_CONFIDENTIAL -- EOXS company-confidential business data:
- Salary, payroll, compensation, incentive, or bonus figures for ANY employee (including Raj's own)
- Investor relations and fundraising
- Company financial statements or bank/accounting data
- Vendor payment terms or contracts with sensitive pricing
- Legal or compliance matters (that are NOT Raj's personal legal matters)

TIER2 -- General, visible company-wide: client implementation notes, support tickets, sales/ops
analysis, internal process documentation, and other everyday professional content -- the default
for anything not clearly TIER1 or TIER2_CONFIDENTIAL, even if Raj is a participant or subject.

When genuinely uncertain between two adjacent levels, prefer the more restrictive one (fail closed):
TIER2_CONFIDENTIAL over TIER2, or TIER1 over TIER2_CONFIDENTIAL if it's plausibly Raj's personal
matter rather than company business.

Wiki page title: {title}

Wiki page content (truncated):
{body}

Answer with exactly one word: TIER1, TIER2_CONFIDENTIAL, or TIER2."""


def _fetch_pending(conn):
    """Every live wiki page -- full re-scan (see module docstring)."""
    with conn.cursor() as cur:
        cur.execute("SELECT id, title, body FROM wiki_pages")
        return cur.fetchall()


async def _classify_one(client, sem, title, body):
    context = _PROMPT.format(title=title, body=(body or "")[:_MAX_CONTEXT_CHARS])
    async with sem:
        for attempt in range(MAX_RETRIES):
            try:
                resp = await client.messages.create(
                    model=_MODEL, max_tokens=8,
                    messages=[{"role": "user", "content": context}],
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
    client = anthropic.AsyncAnthropic(api_key=os.environ["CLASSIFIER_ANTHROPIC_API_KEY"])
    sem = asyncio.Semaphore(CONCURRENCY)
    completed = 0
    counts = {"tier1": 0, "tier2_confidential": 0, "tier2": 0}
    start = time.monotonic()

    async def worker(page):
        nonlocal completed
        verdict = await _classify_one(client, sem, page["title"], page["body"])
        # Synchronous write right after the (awaited, concurrent) LLM call
        # returns -- never interleaved with another await, so sharing one
        # psycopg2 connection across these coroutines is safe: each write
        # fully completes before this coroutine yields control back.
        with conn.cursor() as cur:
            cur.execute("UPDATE wiki_pages SET access_tier = %s WHERE id = %s", (verdict, page["id"]))
        conn.commit()
        counts[verdict] += 1
        completed += 1
        if completed % 100 == 0 or completed == len(items):
            elapsed = time.monotonic() - start
            rate = completed / elapsed if elapsed else 0
            logger.info("progress: %d/%d (%.1f/s)", completed, len(items), rate)

    await asyncio.gather(*(worker(page) for page in items))
    return counts


_RANK_CASE = "CASE access_tier WHEN 'tier1' THEN 3 WHEN 'tier2_confidential' THEN 2 ELSE 1 END"
_TIER_FROM_RANK = "CASE max_rank WHEN 3 THEN 'tier1' WHEN 2 THEN 'tier2_confidential' ELSE 'tier2' END"


def _apply_citation_backstop(conn):
    """Pass 1: upgrade each page to the most restrictive level among its
    resolved raw-source citations, if stricter than its own content
    verdict. Same fail-closed-on-odoo_task_id-collision join as
    promote.py (no client_id on the citation row -- whichever candidate
    across every client sharing that id number is most restrictive wins)."""
    with conn.cursor() as cur:
        cur.execute(
            f"""
            WITH cited AS (
                SELECT wc.wiki_page_id, t.access_tier::text AS tier FROM wiki_citations wc JOIN email_threads t
                  ON wc.source_type = 'email_thread' AND t.id = wc.source_id
                UNION ALL
                SELECT wc.wiki_page_id, t.access_tier::text FROM wiki_citations wc JOIN call_transcripts t
                  ON wc.source_type = 'call_transcript' AND t.id = wc.source_id
                UNION ALL
                SELECT wc.wiki_page_id, t.access_tier::text FROM wiki_citations wc JOIN tickets t
                  ON wc.source_type = 'ticket' AND t.id = wc.source_id
                UNION ALL
                SELECT wc.wiki_page_id, t.access_tier::text FROM wiki_citations wc JOIN implementation_tasks t
                  ON wc.source_type = 'implementation_task' AND t.odoo_task_id = wc.source_id
            ),
            max_cited AS (
                SELECT wiki_page_id,
                    CASE WHEN bool_or(tier = 'tier1') THEN 3
                         WHEN bool_or(tier = 'tier2_confidential') THEN 2
                         ELSE 1 END AS max_rank
                FROM cited GROUP BY wiki_page_id
            )
            UPDATE wiki_pages wp SET access_tier = ({_TIER_FROM_RANK})::access_tier
            FROM max_cited mc
            WHERE wp.id = mc.wiki_page_id AND mc.max_rank > ({_RANK_CASE.replace('access_tier', 'wp.access_tier')})
            """
        )
        upgraded = cur.rowcount
    conn.commit()
    return upgraded


def _propagate_wiki_to_wiki(conn):
    """Pass 2: propagate the most restrictive cited-wiki-page level along
    wiki-page-cites-wiki-page citation edges to a fixed point. Monotonic
    (only ever tightens) and bounded, so this always terminates."""
    total = 0
    while True:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                WITH cited AS (
                    SELECT wc.wiki_page_id, cited.access_tier::text AS tier
                    FROM wiki_citations wc JOIN wiki_pages cited
                      ON wc.source_type = 'wiki_page' AND cited.id = wc.source_id
                ),
                max_cited AS (
                    SELECT wiki_page_id,
                        CASE WHEN bool_or(tier = 'tier1') THEN 3
                             WHEN bool_or(tier = 'tier2_confidential') THEN 2
                             ELSE 1 END AS max_rank
                    FROM cited GROUP BY wiki_page_id
                )
                UPDATE wiki_pages wp SET access_tier = ({_TIER_FROM_RANK})::access_tier
                FROM max_cited mc
                WHERE wp.id = mc.wiki_page_id AND mc.max_rank > ({_RANK_CASE.replace('access_tier', 'wp.access_tier')})
                """
            )
            n = cur.rowcount
        conn.commit()
        if n == 0:
            break
        total += n
    return total


def main():
    conn = get_live_conn()
    try:
        items = _fetch_pending(conn)
        logger.info("pending wiki tier classification: %d pages", len(items))
        if items:
            counts = asyncio.run(_run(conn, items))
            logger.info(
                "content classification: %d -> tier1, %d -> tier2_confidential, %d -> tier2",
                counts["tier1"], counts["tier2_confidential"], counts["tier2"],
            )

        upgraded_a = _apply_citation_backstop(conn)
        logger.info("citation backstop: %d pages upgraded", upgraded_a)

        upgraded_b = _propagate_wiki_to_wiki(conn)
        logger.info("wiki-to-wiki propagation: %d more pages upgraded", upgraded_b)

        with conn.cursor() as cur:
            cur.execute("SELECT access_tier, count(*) FROM wiki_pages GROUP BY access_tier")
            for row in cur.fetchall():
                logger.info("final: %s = %d", row["access_tier"], row["count"])
    finally:
        conn.close()


if __name__ == "__main__":
    main()
