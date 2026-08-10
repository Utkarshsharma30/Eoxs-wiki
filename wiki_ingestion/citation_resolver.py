"""One-time mechanical re-resolution pass for wiki_citations rows stuck at
source_type='unresolved' -- free-text frontmatter 'sources' entries from
the old markdown-vault import that the original loader's naive parser
never matched to a real DB row (711 of 1,122 total citations, found while
building the access-tier system -- see session discussion).

Pattern-based, NOT LLM-based, deliberately: a citation only gets resolved
if (a) its source_ref_raw parses into a recognizable
{source_account}-{date}-{slug} shape (the old vault's file-naming
convention) AND (b) a high-confidence keyword-overlap match against a
real email_threads row is found within a date window. No fuzzy guessing
beyond that -- a citation that doesn't clear the confidence bar stays
unresolved rather than risk a wrong attribution (a false citation is
worse than an honest "we don't know").

Scope of this first pass: source_ref_raw values with a raj_gmail/
ron_gmail/remya_gmail/support_zoho prefix and an embedded date (~11% of
the unresolved set in the sample checked). The remaining unresolved refs
are either genuinely generic category labels ("proposal", "user-list")
that were never precise citations to begin with -- see
mark_unresolvable() below, which reclassifies those explicitly rather
than leaving them looking like a fixable backlog forever -- or don't fit
this pattern and are left for a future, more expensive pass (LLM-assisted
content matching) if that's ever wanted.
"""
import logging
import re
from datetime import date, timedelta

from ingestion.db import get_live_conn

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("wiki_ingestion.citation_resolver")

_PATTERN = re.compile(
    r"^(raj_gmail|ron_gmail|remya_gmail|support_zoho)-(\d{4})-(\d{1,2})(?:-(\d{1,2}))?-(.+)$"
)

# Generic category labels seen in the unresolved set that were never precise
# citations (no specific row could ever satisfy them) -- confirmed by manual
# sampling, not guessed. Extend this list if a future audit finds more.
_UNRESOLVABLE_LABELS = {
    "proposal", "user-list", "technical-asset", "invoice-history", "icp-asset",
    "client-proposals", "internal-email-archive", "internal-notes", "misc",
    "unclassified", "attachment", "attachments",
}

_STOPWORDS = {
    "the", "a", "an", "and", "or", "for", "to", "of", "in", "on", "at", "re",
    "fw", "fwd", "with", "from", "is", "was", "no", "subject",
}


def _slug_words(slug):
    return {w for w in slug.lower().replace("_", "-").split("-") if w and w not in _STOPWORDS}


def _fetch_unresolved(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT id, wiki_page_id, source_ref_raw FROM wiki_citations WHERE source_type = 'unresolved'")
        return cur.fetchall()


def _candidates(conn, source_account, year, month, day):
    if day:
        center = date(int(year), int(month), int(day))
        start, end = center - timedelta(days=3), center + timedelta(days=3)
    else:
        start = date(int(year), int(month), 1)
        end = date(int(year), int(month) + 1, 1) if int(month) < 12 else date(int(year) + 1, 1, 1)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, subject FROM email_threads
            WHERE source_account = %s AND EXISTS (
                SELECT 1 FROM unnest(thread_dates) d WHERE d::date BETWEEN %s AND %s
            )
            """,
            (source_account, start, end),
        )
        return cur.fetchall()


def resolve_pass(conn):
    """Returns (resolved_count, attempted_count)."""
    unresolved = _fetch_unresolved(conn)
    attempted = 0
    resolved = 0
    for row in unresolved:
        m = _PATTERN.match(row["source_ref_raw"])
        if not m:
            continue
        attempted += 1
        source_account, year, month, day, slug = m.groups()
        target_words = _slug_words(slug)
        if len(target_words) < 2:
            continue  # too little signal to match confidently

        candidates = _candidates(conn, source_account, year, month, day)
        best, best_score = None, 0.0
        for c in candidates:
            subject_words = _slug_words((c["subject"] or "").replace(" ", "-"))
            if not subject_words:
                continue
            overlap = len(target_words & subject_words)
            score = overlap / len(target_words)
            if score > best_score:
                best, best_score = c, score

        # Require both a high overlap fraction AND at least 2 real words matched --
        # avoids accepting a match on one common short word alone.
        if best and best_score >= 0.6 and len(target_words & _slug_words((best["subject"] or "").replace(" ", "-"))) >= 2:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE wiki_citations SET source_type = 'email_thread', source_id = %s WHERE id = %s",
                    (best["id"], row["id"]),
                )
            conn.commit()
            resolved += 1
            logger.info("resolved citation %d: %r -> email_thread id=%d (score=%.2f)",
                        row["id"], row["source_ref_raw"], best["id"], best_score)
    return resolved, attempted


def mark_unresolvable(conn):
    """Reclassifies citations whose source_ref_raw is a known generic
    category label (never a precise citation to begin with) to
    'unresolvable', distinguishing "permanently can't be resolved" from
    "not yet resolved" -- an honest signal instead of a backlog that never
    shrinks. Doesn't touch source_id (stays NULL)."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE wiki_citations SET source_type = 'unresolvable' "
            "WHERE source_type = 'unresolved' AND lower(source_ref_raw) = ANY(%s)",
            (list(_UNRESOLVABLE_LABELS),),
        )
        n = cur.rowcount
    conn.commit()
    return n


def main():
    conn = get_live_conn()
    try:
        resolved, attempted = resolve_pass(conn)
        logger.info("pattern-matched %d/%d candidate citations to real rows", resolved, attempted)

        marked = mark_unresolvable(conn)
        logger.info("marked %d citations as permanently unresolvable (generic category labels)", marked)

        with conn.cursor() as cur:
            cur.execute("SELECT source_type, count(*) FROM wiki_citations GROUP BY source_type ORDER BY source_type")
            for row in cur.fetchall():
                logger.info("final: %s = %d", row["source_type"], row["count"])
    finally:
        conn.close()


if __name__ == "__main__":
    main()
