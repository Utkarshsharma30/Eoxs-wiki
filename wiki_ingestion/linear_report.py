"""Wiki-ingestion Linear reporting on the EDB team (Phase 6 -- the phase
plan confirmed with the user: schema+detection, write-MCP+one-category,
fan-out, consolidation, review sweep, THEN Linear). Reuses the low-level
Linear GraphQL client (_get_team_id/_create_issue/team-id cache) from
ingestion/linear_report.py rather than duplicating it -- same EDB team,
same API key, only the report content differs.

One issue per completed PHASE RUN (a Phase 3 cycle, a Phase 4
consolidation pass, a Phase 5 review sweep, or a promotion batch) --
NOT a unified end-to-end issue, because these phases currently run as
separate driver invocations (run_cycle.py, run_consolidation.py,
run_review.py, promote.py), not one orchestrated call. If/when those get
unified into a single scheduled job (the still-outstanding "recurring
6-hourly job" piece), revisit this to report once per full pass with
child issues per phase via Linear's parentId, closer to wiki-agent's WIK
board structure -- HANDOFF.md section 8 flagged that as the eventual
shape but left it unpinned.
"""
import logging

from ingestion.db import get_live_conn
from ingestion.linear_report import _create_issue

logger = logging.getLogger("wiki_ingestion.linear_report")


def _safe_report(build_fn, *args):
    """Never raises -- a Linear reporting failure should never fail or
    mask an otherwise-successful ingestion/consolidation/review/promotion
    run (same discipline as ingestion/linear_report.py)."""
    try:
        title, body = build_fn(*args)
        created = _create_issue(title, body)
        if not created.get("success"):
            logger.warning("Linear issueCreate returned success=false: %s", created)
        else:
            logger.info("Linear report created: %s", created["issue"]["identifier"])
    except Exception as e:
        logger.warning("Linear report failed (result unaffected): %s", e)


def _pages_by_source_kind(cycle_id):
    conn = get_live_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT source_kind, count(*) AS n FROM wiki_staging.wiki_pages WHERE cycle_id = %s GROUP BY source_kind",
                (cycle_id,),
            )
            return {r["source_kind"]: r["n"] for r in cur.fetchall()}
    finally:
        conn.close()


def _build_cycle_report(result):
    cycle_id = result["cycle_id"]
    pages = _pages_by_source_kind(cycle_id)
    batches = result.get("batches", [])

    lines = [
        f"**Wiki-ingestion cycle {cycle_id} — status: {result['status']}**",
        "",
        f"Partitions: {result.get('partitions_total', '?')} · Batches: {result.get('batches_total', len(batches))} "
        f"· Failed: {result.get('batches_failed', 0)}",
        "",
        "| Batch (source_kind) | Rows | Status | Pages drafted (source_kind total) |",
        "|---|---|---|---|",
    ]
    for b in batches:
        base_kind = b["source_kind"].split("[")[0]
        lines.append(f"| {b['source_kind']} | {b['row_count']} | {b['status']} | {pages.get(base_kind, 0)} |")

    total_pages = sum(pages.values())
    lines += ["", f"**Total staging pages drafted this cycle: {total_pages}** (landed in wiki_staging.wiki_pages)"]

    title = f"Wiki-ingestion cycle {cycle_id} — {result['status']} — {total_pages} page(s) drafted"
    return title, "\n".join(lines)


def report_cycle(result):
    """Skips reporting for a no-op cycle (nothing changed since last
    detection) -- matches detect.run_detection's own "no empty partitions
    on the board" philosophy. Real, informative failures still get
    reported: a cycle with batches_failed>0 always has batches_total>0."""
    if not result.get("batches_total"):
        logger.info("Cycle %s: no partitions had changes -- skipping Linear report", result.get("cycle_id"))
        return
    _safe_report(_build_cycle_report, result)


def _build_consolidation_report(result):
    results = result.get("results", [])
    lines = [
        f"**Wiki-ingestion consolidation pass — {result['groups_total']} duplicate group(s), "
        f"{result['groups_failed']} failed**",
        "",
        "| Title | Pages merged | Status |",
        "|---|---|---|",
    ]
    for r in results:
        status = "merged" if r["ok"] else f"FAILED — {r.get('error', '')[:200]}"
        lines.append(f"| {r['title']} | {len(r['page_ids'])} | {status} |")

    title = f"Wiki-ingestion consolidation — {result['groups_total']} group(s), {result['groups_failed']} failed"
    return title, "\n".join(lines)


def report_consolidation(result):
    if not result.get("groups_total"):
        logger.info("Consolidation: no duplicate groups -- skipping Linear report")
        return
    _safe_report(_build_consolidation_report, result)


def _build_review_report(result):
    conn = get_live_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status, count(*) AS n FROM wiki_staging.wiki_pages WHERE status IN ('reviewed','rejected') GROUP BY status"
            )
            totals = {r["status"]: r["n"] for r in cur.fetchall()}
            cur.execute(
                "SELECT id, title, review_notes FROM wiki_staging.wiki_pages WHERE status = 'rejected' ORDER BY id"
            )
            rejected = cur.fetchall()
    finally:
        conn.close()

    lines = [
        f"**Wiki-ingestion review sweep — {result['drafts_total']} draft(s) reviewed, "
        f"{result['chunks_failed']} chunk(s) failed**",
        "",
        f"Current totals across all staging: **{totals.get('reviewed', 0)} reviewed** (awaiting promotion approval), "
        f"**{totals.get('rejected', 0)} rejected**.",
    ]
    if rejected:
        lines += ["", "| Rejected page | Reason |", "|---|---|"]
        for r in rejected:
            reason = (r["review_notes"] or "").replace("\n", " ")[:300]
            lines.append(f"| {r['title']} (id={r['id']}) | {reason} |")

    title = f"Wiki-ingestion review sweep — {totals.get('reviewed', 0)} reviewed, {totals.get('rejected', 0)} rejected"
    return title, "\n".join(lines)


def report_review(result):
    if not result.get("drafts_total"):
        logger.info("Review sweep: no draft pages -- skipping Linear report")
        return
    _safe_report(_build_review_report, result)


def _build_promotion_report(result):
    lines = [
        f"**Wiki-ingestion promotion — {result['succeeded']}/{result['attempted']} promoted to live**",
        "",
        f"Newly resolved links: {result.get('newly_resolved_links', 0)}",
    ]
    if result.get("failed"):
        lines += ["", "| Failed |", "|---|"]
        for f in result["failed"]:
            lines.append(f"| {f} |")

    title = f"Wiki-ingestion promotion — {result['succeeded']}/{result['attempted']} live"
    return title, "\n".join(lines)


def report_promotion(result):
    if not result.get("attempted"):
        logger.info("Promotion: nothing to promote -- skipping Linear report")
        return
    _safe_report(_build_promotion_report, result)
