"""Phase 5's promotion step: moves a 'reviewed' staging draft into
public.wiki_pages for real.

2026-08 change: promote_reviewed_pages() is now called automatically by
run_review.py at the end of every scheduled review sweep -- a page the
review agent marks 'reviewed' goes live with no further human gate.
This was a deliberate decision (not every review outcome gets this
trust: 'rejected' pages are never touched by this module at all, and
still require a human to decide what happens to them next). The
functions below are unchanged from when promotion was a manual-only
step; promote_page() still works standalone for a single id if ever
needed by hand.

live_page_id NULL on the staging row means CREATE (INSERT into
wiki_pages); non-NULL means UPDATE that existing live row. Either way,
citations/flags/links are fully replaced on the live page from the
staging draft's current set (not merged/appended) -- the staging draft
is the authoritative, reviewed version of this content at promotion
time.
"""
import json
from datetime import date

from ingestion.db import get_live_conn
from wiki_ingestion.linear_report import report_promotion, report_pending_drafts_board


def _fetch_reviewed(conn, staging_page_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM wiki_staging.wiki_pages WHERE id = %s AND status = 'reviewed'", (staging_page_id,)
        )
        return cur.fetchone()


def promote_page(staging_page_id):
    """Promotes one reviewed staging draft to public.wiki_pages. Returns
    {staging_page_id, live_page_id, action: 'created'|'updated'} on
    success, or {"error": ...} without touching anything on failure."""
    conn = get_live_conn()
    try:
        draft = _fetch_reviewed(conn, staging_page_id)
        if not draft:
            return {"error": f"no reviewed-status staging page with id={staging_page_id}"}

        today = date.today()
        with conn.cursor() as cur:
            if draft["live_page_id"] is None:
                cur.execute(
                    """
                    INSERT INTO wiki_pages (
                        title, page_type, entity_class, tags, sources_raw,
                        created_date, updated_date, updated_raw, generated_hash, body
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        draft["title"], draft["page_type"], draft["entity_class"], draft["tags"],
                        draft["sources_raw"], draft["created_date"] or today, today,
                        draft["updated_raw"], draft["generated_hash"], draft["body"],
                    ),
                )
                live_id = cur.fetchone()["id"]
                action = "created"
            else:
                live_id = draft["live_page_id"]
                cur.execute(
                    """
                    UPDATE wiki_pages SET
                        title = %s, page_type = %s, entity_class = %s, tags = %s, sources_raw = %s,
                        updated_date = %s, updated_raw = %s, generated_hash = %s, body = %s, updated_at = now()
                    WHERE id = %s
                    """,
                    (
                        draft["title"], draft["page_type"], draft["entity_class"], draft["tags"],
                        draft["sources_raw"], today, draft["updated_raw"], draft["generated_hash"],
                        draft["body"], live_id,
                    ),
                )
                action = "updated"

            cur.execute("DELETE FROM wiki_citations WHERE wiki_page_id = %s", (live_id,))
            cur.execute("DELETE FROM wiki_flags WHERE wiki_page_id = %s", (live_id,))
            cur.execute("DELETE FROM wiki_links WHERE from_page_id = %s", (live_id,))

            cur.execute(
                """
                INSERT INTO wiki_citations (wiki_page_id, source_type, source_id, source_ref_raw)
                SELECT %s, source_type, source_id, source_ref_raw
                FROM wiki_staging.wiki_citations WHERE wiki_page_id = %s
                """,
                (live_id, staging_page_id),
            )
            cur.execute(
                """
                INSERT INTO wiki_flags (wiki_page_id, flag_type, text)
                SELECT %s, flag_type, text
                FROM wiki_staging.wiki_flags WHERE wiki_page_id = %s
                """,
                (live_id, staging_page_id),
            )
            cur.execute(
                """
                INSERT INTO wiki_links (from_page_id, to_page_id, to_title_raw, display_text, context_snippet)
                SELECT %s, (SELECT id FROM wiki_pages WHERE title = sl.to_title_raw LIMIT 1),
                       sl.to_title_raw, sl.display_text, sl.context_snippet
                FROM wiki_staging.wiki_links sl WHERE sl.from_page_id = %s
                """,
                (live_id, staging_page_id),
            )

            # Access-tier: MAX (most restrictive) of every citation just copied
            # above, across the 3-level scheme (tier1 = Raj-personal >
            # tier2_confidential = company-confidential > tier2 = general).
            # Reliable here (unlike the old vault-imported pages) because the
            # agent resolves source_id against a real row before ever calling
            # add_staging_citation -- see agent_mcp_server.py, which only
            # allows the four real raw source_types, no 'unresolved'/
            # 'wiki_page' escape hatch. Implementation-task citations store
            # source_id as odoo_task_id, which collides across clients (no
            # client_id on the citation row) -- the join below doesn't try to
            # disambiguate, it just fails closed: whichever cited candidate
            # (across every client sharing that id number) is most
            # restrictive wins.
            cur.execute(
                """
                SELECT t.access_tier::text AS tier FROM wiki_citations wc JOIN email_threads t
                  ON wc.source_type = 'email_thread' AND t.id = wc.source_id
                WHERE wc.wiki_page_id = %s
                UNION
                SELECT t.access_tier::text FROM wiki_citations wc JOIN call_transcripts t
                  ON wc.source_type = 'call_transcript' AND t.id = wc.source_id
                WHERE wc.wiki_page_id = %s
                UNION
                SELECT t.access_tier::text FROM wiki_citations wc JOIN tickets t
                  ON wc.source_type = 'ticket' AND t.id = wc.source_id
                WHERE wc.wiki_page_id = %s
                UNION
                SELECT t.access_tier::text FROM wiki_citations wc JOIN implementation_tasks t
                  ON wc.source_type = 'implementation_task' AND t.odoo_task_id = wc.source_id
                WHERE wc.wiki_page_id = %s
                """,
                (live_id, live_id, live_id, live_id),
            )
            cited_tiers = {row["tier"] for row in cur.fetchall()}
            if "tier1" in cited_tiers:
                access_tier = "tier1"
            elif "tier2_confidential" in cited_tiers:
                access_tier = "tier2_confidential"
            else:
                access_tier = "tier2"
            cur.execute("UPDATE wiki_pages SET access_tier = %s WHERE id = %s", (access_tier, live_id))

            cur.execute(
                "UPDATE wiki_staging.wiki_pages SET status = 'promoted', live_page_id = %s, updated_at = now() WHERE id = %s",
                (live_id, staging_page_id),
            )
        conn.commit()
        return {"staging_page_id": staging_page_id, "live_page_id": live_id, "action": action}
    except Exception as e:
        conn.rollback()
        return {"error": f"{type(e).__name__}: {e}"}
    finally:
        conn.close()


def _reresolve_unresolved_links():
    """Re-checks every unresolved live wiki_link (to_page_id IS NULL)
    against current wiki_pages titles -- promoting a batch in id order
    means an earlier page's link to a later-promoted page starts out
    unresolved; this fixes those up in one pass after the whole batch
    lands, mirroring loaders/load_wiki.py's full-recompute approach."""
    conn = get_live_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE wiki_links l SET to_page_id = wp.id
                FROM wiki_pages wp
                WHERE l.to_page_id IS NULL AND wp.title = l.to_title_raw
                """
            )
            resolved = cur.rowcount
        conn.commit()
        return resolved
    finally:
        conn.close()


def promote_reviewed_pages():
    """Promotes every currently 'reviewed' staging draft. Returns a summary
    dict; a single page's promotion failure doesn't stop the rest (each
    runs in its own transaction) -- it's recorded and left 'reviewed' for
    a retry."""
    conn = get_live_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, title FROM wiki_staging.wiki_pages WHERE status = 'reviewed' ORDER BY id")
            rows = cur.fetchall()
    finally:
        conn.close()

    titles_by_id = {r["id"]: r["title"] for r in rows}
    results = [promote_page(r["id"]) for r in rows]
    newly_resolved_links = _reresolve_unresolved_links()

    succeeded_list = [
        {"staging_page_id": r["staging_page_id"], "live_page_id": r["live_page_id"], "action": r["action"],
         "title": titles_by_id.get(r["staging_page_id"])}
        for r in results if "error" not in r
    ]
    result = {
        "attempted": len(rows),
        "succeeded": len(succeeded_list),
        "succeeded_list": succeeded_list,
        "failed": [r for r in results if "error" in r],
        "newly_resolved_links": newly_resolved_links,
    }
    report_promotion(result)
    report_pending_drafts_board()
    return result


if __name__ == "__main__":
    print(json.dumps(promote_reviewed_pages(), indent=2))
