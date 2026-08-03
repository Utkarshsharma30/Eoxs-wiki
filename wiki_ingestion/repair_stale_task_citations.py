"""One-off repair for the incident fixed in commit 2d1e59c: 30 staging
pages (client_discount-pipe-steel, client_sabre-alloys) were rejected by
Phase 5's review sweep because their implementation_task citations cited
a serial `id` that had gone stale (odoo_fetcher.py's full-refresh
reassigned fresh ids mid-cycle) -- content was verified accurate by the
reviewer, only the citations were dead.

Rather than trust the reviewer's prose-embedded corrected ids (not
uniformly structured) or an assumed constant per-client offset (true in
every sampled case, but not worth depending on), this re-derives each
citation's correct target independently: parses the task name out of
source_ref_raw, looks it up fresh in implementation_tasks scoped to the
page's client, and repairs source_id to that row's odoo_task_id. A
citation that doesn't resolve to exactly one match is left alone and
reported -- never guessed.

Only touches the specific rejected pages from this incident (source_kind
IN the two affected clients, status='rejected'), not a general-purpose
tool -- the underlying bug is fixed at the source (see commit 2d1e59c),
so this shouldn't be needed again.
"""
import json
import re

from ingestion.db import get_live_conn

AFFECTED_SOURCE_KINDS = ["client_discount-pipe-steel", "client_sabre-alloys"]

# Formats seen in the wild: "task 8907: CRM Module", "implementation task
# id=10069: handle odoo error and type send error email.", and some with a
# " (stage: X)" annotation appended that isn't part of the real task_name
# column -- stripped separately below.
NAME_RE = re.compile(r":\s*(.+?)\.?\s*$")
STAGE_SUFFIX_RE = re.compile(r"\s*\(stage:[^)]*\)\s*$", re.IGNORECASE)


def _extract_task_name(source_ref_raw):
    m = NAME_RE.search(source_ref_raw or "")
    if not m:
        return None
    name = m.group(1).strip()
    name = STAGE_SUFFIX_RE.sub("", name).strip()
    return name or None


def repair():
    conn = get_live_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, source_kind FROM wiki_staging.wiki_pages
                WHERE source_kind = ANY(%s) AND status = 'rejected'
                ORDER BY id
                """,
                (AFFECTED_SOURCE_KINDS,),
            )
            pages = cur.fetchall()

        repaired_pages, unresolved, page_results = [], [], []
        for page in pages:
            client_slug = page["source_kind"].removeprefix("client_")
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, source_id, source_ref_raw FROM wiki_staging.wiki_citations
                    WHERE wiki_page_id = %s AND source_type = 'implementation_task'
                    """,
                    (page["id"],),
                )
                citations = cur.fetchall()

            page_ok = True
            fixes = []
            for c in citations:
                task_name = _extract_task_name(c["source_ref_raw"])
                if not task_name:
                    page_ok = False
                    unresolved.append({"page_id": page["id"], "citation_id": c["id"], "reason": "couldn't parse task name from source_ref_raw", "source_ref_raw": c["source_ref_raw"]})
                    continue
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT it.odoo_task_id FROM implementation_tasks it JOIN clients cl ON cl.id = it.client_id
                        WHERE cl.slug = %s AND it.task_name = %s
                        """,
                        (client_slug, task_name),
                    )
                    matches = cur.fetchall()
                if len(matches) != 1:
                    page_ok = False
                    unresolved.append({"page_id": page["id"], "citation_id": c["id"], "reason": f"{len(matches)} matches for task_name={task_name!r} in client={client_slug}", "old_source_id": c["source_id"]})
                    continue
                new_id = matches[0]["odoo_task_id"]
                fixes.append((c["id"], c["source_id"], new_id, task_name))

            if page_ok and fixes:
                with conn.cursor() as cur:
                    for citation_id, old_id, new_id, task_name in fixes:
                        cur.execute(
                            "UPDATE wiki_staging.wiki_citations SET source_id = %s, source_ref_raw = %s WHERE id = %s",
                            (new_id, f"implementation task odoo_task_id={new_id}: {task_name}", citation_id),
                        )
                    cur.execute(
                        "UPDATE wiki_staging.wiki_pages SET status = 'draft', review_notes = NULL, updated_at = now() WHERE id = %s",
                        (page["id"],),
                    )
                conn.commit()
                repaired_pages.append(page["id"])
                page_results.append({"page_id": page["id"], "citations_fixed": len(fixes)})
            else:
                conn.rollback()
                page_results.append({"page_id": page["id"], "citations_fixed": 0, "status": "left rejected -- see unresolved"})

        return {
            "pages_considered": len(pages),
            "pages_repaired": len(repaired_pages),
            "repaired_page_ids": repaired_pages,
            "unresolved": unresolved,
            "page_results": page_results,
        }
    finally:
        conn.close()


if __name__ == "__main__":
    print(json.dumps(repair(), indent=2))
