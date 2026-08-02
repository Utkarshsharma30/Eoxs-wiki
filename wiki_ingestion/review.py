"""Phase 5 review-sweep support: finds staging drafts awaiting review.
Scoped the same way as Phase 4's duplicate detection -- across ALL
staging drafts, not one cycle, since Phase 5 runs on a recurring schedule
and a draft can sit unreviewed across multiple ingestion cycles.
"""
from ingestion.db import get_live_conn

# Rows per review sub-agent call. Smaller than Phase 3's ingestion
# CHUNK_SIZE (25): reviewing a page means spot-checking its citations
# against real raw source rows via extra read-tool calls, so each page
# costs more tool calls here than during ingestion.
REVIEW_CHUNK_SIZE = 15


def find_draft_pages():
    conn = get_live_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, title, page_type FROM wiki_staging.wiki_pages WHERE status = 'draft' ORDER BY id"
            )
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def chunk_rows(rows, size=REVIEW_CHUNK_SIZE):
    return [rows[i:i + size] for i in range(0, len(rows), size)]
