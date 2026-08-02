"""Phase 4 duplicate-group detection: finds staging drafts that share the
exact title (case/whitespace-insensitive) and haven't been promoted or
merged yet. Scoped across ALL staging drafts, not one cycle -- staging
can accumulate un-promoted drafts across multiple ingestion cycles (Phase
5 runs this every 6 hours), and a duplicate can just as easily span two
different cycles as two chunks within one.

Exact-title matching, not fuzzy similarity: the duplicates actually
observed after the first real backfill (cycle 6, 51/217 pages) were all
byte-identical titles -- the same recurring sub-topic (e.g. "Eastern
States Steel -- ERP Configuration Requests") independently regenerated
by different chunks of that client's implementation-task history.
Genuinely different topics got genuinely different titles; there was no
evidence of near-duplicate titles needing fuzzy matching, so it isn't
built until it's needed.
"""
from ingestion.db import get_live_conn


def find_duplicate_groups():
    conn = get_live_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT min(title) AS title, array_agg(id ORDER BY id) AS page_ids
                FROM wiki_staging.wiki_pages
                WHERE status = 'draft'
                GROUP BY lower(trim(title))
                HAVING count(*) > 1
                ORDER BY min(title)
                """
            )
            return [{"title": r["title"], "page_ids": r["page_ids"]} for r in cur.fetchall()]
    finally:
        conn.close()
