"""Wiki-ingestion Linear reporting on the EDB team (Phase 6).

2026-08 redesign, per explicit request for complete per-sub-agent
transparency after a real incident where a broken MCP connection caused
~2 days of silent drafting/review failures that every existing report
showed as healthy (see headless_agent.py's incident note). The old model
reported one issue per COMPLETED PHASE RUN, aggregated -- not granular
enough to see which specific sub-agent invocation failed, or to review
page content without a database connection.

New model: one PARENT issue per phase run (a cycle, a consolidation
pass, a review sweep), created the moment the run STARTS (so a stuck or
slow run is visible while in flight, not just after) and updated when it
finishes. One CHILD issue (Linear sub-issue, via parentId) per actual
`claude -p` sub-agent invocation -- one per Phase 3 chunk, one per Phase
4 duplicate group, one per Phase 5 review chunk -- created before that
specific sub-agent runs and updated immediately after with its full,
real outcome: exactly which raw rows/pages it was given, and either the
full body text of every page it created/updated/reviewed, or an
explicit, specific failure reason (never a bare "failed"). A batch that
never touches a single wiki tool is reported as FAILED, not silently
Done -- see headless_agent._looks_like_mcp_startup_race.

Reuses the low-level Linear GraphQL client (_get_team_id/_create_issue/
_update_issue/team-id cache) from ingestion/linear_report.py rather than
duplicating it -- same EDB team, same API key.
"""
import logging
from collections import defaultdict

from ingestion.db import get_live_conn
from ingestion.linear_report import _create_issue, _update_issue
from ingestion.state import now_utc

logger = logging.getLogger("wiki_ingestion.linear_report")


def _safe(fn, *args, **kwargs):
    """Never raises -- a Linear reporting failure should never fail or mask
    an otherwise-successful ingestion/consolidation/review/promotion run."""
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        logger.warning("Linear report step failed (result unaffected): %s", e)
        return None


def _create(title, body, state_name="Todo", parent_id=None):
    created = _create_issue(title, body, state_name=state_name, parent_id=parent_id)
    if not created.get("success"):
        logger.warning("Linear issueCreate returned success=false: %s", created)
        return None
    return created["issue"]["id"]


def _update(issue_id, title, body, state_name):
    if not issue_id:
        return
    updated = _update_issue(issue_id, title, body, state_name=state_name)
    if not updated.get("success"):
        logger.warning("Linear issueUpdate returned success=false: %s", updated)


def _fmt_ai_failure(ai_result):
    """A specific, actionable failure reason -- never a bare 'failed'."""
    if ai_result.get("mcp_never_connected"):
        return (
            f"The wiki MCP server never finished connecting after {ai_result.get('attempts')} "
            f"attempt(s) (status stayed 'pending' the whole session) -- none of the wiki tools "
            f"were available, so nothing could be read or written. This is an infrastructure "
            f"issue with the `claude -p` sub-agent's MCP connection, not a content problem. "
            f"See wiki_ingestion/headless_agent.py's incident note."
        )
    stderr = (ai_result.get("stderr") or "").strip()
    if stderr:
        return f"Process error (returncode={ai_result.get('returncode')}):\n\n```\n{stderr[-1500:]}\n```"
    return f"Process exited with returncode={ai_result.get('returncode')}, no stderr captured."


# ---------------------------------------------------------------------------
# Phase 3 -- detect & draft. One parent per cycle, one child task per chunk.
# ---------------------------------------------------------------------------

def start_cycle_parent(cycle_id):
    title = f"Wiki-ingestion cycle {cycle_id} — running"
    body = (
        f"Cycle {cycle_id} started {now_utc().strftime('%Y-%m-%d %H:%M:%S UTC')}. "
        f"Each source/chunk below runs as its own sub-task (see Linear's sub-issues panel) -- "
        f"open one to see exactly which raw rows it was given and what it produced."
    )
    return _safe(_create, title, body, "Todo")


def finish_cycle_parent(parent_issue_id, cycle_id, summary):
    if not parent_issue_id:
        return
    total_pages = _safe(_cycle_pages_drafted, cycle_id) or 0
    title = f"Wiki-ingestion cycle {cycle_id} — {summary['status']} — {total_pages} page(s) drafted"
    body = (
        f"**Status: {summary['status']}**\n\n"
        f"Partitions: {summary.get('partitions_total', '?')} · Batches: {summary.get('batches_total', 0)} "
        f"· Failed: {summary.get('batches_failed', 0)} · Pages drafted/updated: {total_pages}\n\n"
        f"Open the sub-issues below for exactly what each batch did."
    )
    state = "Todo" if summary.get("batches_failed") else "Done"
    _safe(_update, parent_issue_id, title, body, state)


def _cycle_pages_drafted(cycle_id):
    conn = get_live_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM wiki_staging.wiki_pages WHERE cycle_id = %s", (cycle_id,))
            return cur.fetchone()["n"]
    finally:
        conn.close()


def _row_label_generic(row, source_kind):
    """Mirrors run_agent._row_summary without importing a private name
    across modules -- kept in sync manually, it's a small formatting
    helper, not logic worth sharing a real interface over."""
    if source_kind == "tickets":
        return f"ticket id={row['id']} {row.get('ticket_number', '')}: {row.get('subject', '')}"
    if source_kind.startswith("client_"):
        return f"implementation task odoo_task_id={row['odoo_task_id']}: {row.get('task_name', '')}"
    if source_kind == "calls":
        return f"call id={row['id']} ({row.get('source', '')}): {row.get('meeting_title', '')}"
    return f"email thread id={row['id']}: {row.get('subject', '')}"


def start_batch_task(parent_issue_id, cycle_id, source_kind, chunk_index, total_chunks, rows):
    if not parent_issue_id:
        return None
    label = f"{source_kind}[{chunk_index}/{total_chunks}]" if total_chunks > 1 else source_kind
    title = f"Cycle {cycle_id} · draft · {label} · {len(rows)} row(s) — running"
    row_lines = "\n".join(f"- {_row_label_generic(r, source_kind)}" for r in rows)
    body = (
        f"**Phase**: 3 — detect & draft\n"
        f"**Cycle**: {cycle_id}  ·  **Source**: {source_kind}\n\n"
        f"**Raw rows in this batch ({len(rows)}):**\n\n{row_lines}\n"
    )
    return _safe(_create, title, body, "Todo", parent_issue_id)


def _pages_touched_since(cycle_id, source_kind, since_ts):
    conn = get_live_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id, title, page_type, body, created_at, updated_at
                   FROM wiki_staging.wiki_pages
                   WHERE cycle_id = %s AND source_kind = %s AND (created_at >= %s OR updated_at >= %s)
                   ORDER BY id""",
                (cycle_id, source_kind, since_ts, since_ts),
            )
            return cur.fetchall()
    finally:
        conn.close()


def finish_batch_task(issue_id, cycle_id, source_kind, chunk_index, total_chunks, ai_result, since_ts):
    if not issue_id:
        return
    label = f"{source_kind}[{chunk_index}/{total_chunks}]" if total_chunks > 1 else source_kind
    if not ai_result["ok"]:
        title = f"Cycle {cycle_id} · draft · {label} — FAILED"
        body = f"**Status: FAILED** after {ai_result.get('attempts')} attempt(s).\n\n{_fmt_ai_failure(ai_result)}\n\nThis batch's rows were NOT drafted and need a retry."
        _safe(_update, issue_id, title, body, "Todo")
        return

    pages = _safe(_pages_touched_since, cycle_id, source_kind, since_ts) or []
    if not pages:
        title = f"Cycle {cycle_id} · draft · {label} — 0 pages (nothing worth drafting)"
        body = "**Status: succeeded** — the sub-agent read this batch's rows and deliberately drafted/updated nothing (judged not wiki-worthy, or already covered by an existing page)."
        _safe(_update, issue_id, title, body, "Done")
        return

    title = f"Cycle {cycle_id} · draft · {label} — {len(pages)} page(s) written"
    sections = []
    for p in pages:
        action = "CREATED" if p["created_at"] >= since_ts else "UPDATED"
        sections.append(f"### [{action}] {p['title']}  (staging id {p['id']}, {p['page_type']})\n\n{p['body']}")
    body = f"**Status: succeeded** — {len(pages)} page(s) written this batch.\n\n---\n\n" + "\n\n---\n\n".join(sections)
    _safe(_update, issue_id, title, body, "Done")


# ---------------------------------------------------------------------------
# Phase 4 -- consolidation. One parent per pass, one child task per group.
# ---------------------------------------------------------------------------

def start_consolidation_parent(groups_total):
    title = f"Wiki-ingestion consolidation pass — running ({groups_total} duplicate group(s))"
    body = f"Started {now_utc().strftime('%Y-%m-%d %H:%M:%S UTC')} · {groups_total} duplicate-title group(s) found."
    return _safe(_create, title, body, "Todo")


def finish_consolidation_parent(parent_issue_id, result):
    if not parent_issue_id:
        return
    title = f"Wiki-ingestion consolidation pass — {result['groups_total']} group(s), {result['groups_failed']} failed"
    body = f"**Status**: {result['groups_total'] - result['groups_failed']}/{result['groups_total']} merged successfully. Open the sub-issues below for each group's outcome."
    state = "Todo" if result.get("groups_failed") else "Done"
    _safe(_update, parent_issue_id, title, body, state)


def start_consolidation_task(parent_issue_id, group):
    if not parent_issue_id:
        return None
    title = f"Consolidate · \"{group['title']}\" · {len(group['page_ids'])} duplicates — running"
    body = f"**Phase**: 4 — consolidation\n\n**Duplicate title**: {group['title']}\n**Staging page ids being merged**: {', '.join(str(i) for i in group['page_ids'])}\n"
    return _safe(_create, title, body, "Todo", parent_issue_id)


def finish_consolidation_task(issue_id, group, ai_result):
    if not issue_id:
        return
    title_txt = group["title"]
    if not ai_result["ok"]:
        title = f"Consolidate · \"{title_txt}\" — FAILED"
        body = f"**Status: FAILED** after {ai_result.get('attempts')} attempt(s).\n\n{_fmt_ai_failure(ai_result)}\n\nThese duplicates are unmerged and will be retried next pass."
        _safe(_update, issue_id, title, body, "Todo")
        return

    merged = _safe(_get_staging_pages, group["page_ids"]) or []
    surviving = [p for p in merged if p]
    title = f"Consolidate · \"{title_txt}\" — merged"
    if surviving:
        p = surviving[0]
        body = f"**Status: succeeded** — merged into staging id {p['id']}.\n\n### Merged content\n\n{p['body']}"
    else:
        body = "**Status: succeeded** — merge tool reported success, but the kept page could no longer be found (may have already been promoted/rejected since)."
    _safe(_update, issue_id, title, body, "Done")


def _get_staging_pages(ids):
    conn = get_live_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, title, page_type, body FROM wiki_staging.wiki_pages WHERE id = ANY(%s) ORDER BY id",
                (ids,),
            )
            return cur.fetchall()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Phase 5 -- review. One parent per sweep, one child task per chunk.
# ---------------------------------------------------------------------------

def start_review_parent(drafts_total):
    title = f"Wiki-ingestion review sweep — running ({drafts_total} draft(s))"
    body = f"Started {now_utc().strftime('%Y-%m-%d %H:%M:%S UTC')} · {drafts_total} draft page(s) awaiting review."
    return _safe(_create, title, body, "Todo")


def finish_review_parent(parent_issue_id, result):
    if not parent_issue_id:
        return
    totals = _safe(_review_totals) or {}
    title = f"Wiki-ingestion review sweep — {result['drafts_total']} reviewed this run, {result['chunks_failed']} chunk(s) failed"
    body = (
        f"**This run**: {result['drafts_total']} draft(s) processed across {result['chunks_total']} chunk(s), "
        f"{result['chunks_failed']} failed.\n\n"
        f"**Current totals across all staging**: {totals.get('reviewed', 0)} reviewed (awaiting promotion), "
        f"{totals.get('rejected', 0)} rejected, {totals.get('draft', 0)} still draft.\n\n"
        f"Open the sub-issues below for each chunk's page-by-page verdicts."
    )
    state = "Todo" if result.get("chunks_failed") else "Done"
    _safe(_update, parent_issue_id, title, body, state)


def _review_totals():
    conn = get_live_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT status, count(*) AS n FROM wiki_staging.wiki_pages GROUP BY status")
            return {r["status"]: r["n"] for r in cur.fetchall()}
    finally:
        conn.close()


def start_review_task(parent_issue_id, chunk_index, total_chunks, rows):
    if not parent_issue_id:
        return None
    title = f"Review chunk {chunk_index + 1}/{total_chunks} · {len(rows)} page(s) — running"
    row_lines = "\n".join(f"- id={r['id']} [{r['page_type']}] \"{r['title']}\"" for r in rows)
    body = f"**Phase**: 5 — review\n\n**Pages in this chunk ({len(rows)}):**\n\n{row_lines}\n"
    return _safe(_create, title, body, "Todo", parent_issue_id)


def finish_review_task(issue_id, chunk_index, total_chunks, page_ids, ai_result):
    if not issue_id:
        return
    if not ai_result["ok"]:
        title = f"Review chunk {chunk_index + 1}/{total_chunks} — FAILED"
        body = f"**Status: FAILED** after {ai_result.get('attempts')} attempt(s).\n\n{_fmt_ai_failure(ai_result)}\n\nPages {page_ids} were NOT reviewed and remain 'draft' for the next sweep."
        _safe(_update, issue_id, title, body, "Todo")
        return

    pages = _safe(_get_review_verdicts, page_ids) or []
    reviewed = [p for p in pages if p["status"] == "reviewed"]
    rejected = [p for p in pages if p["status"] == "rejected"]
    still_draft = [p for p in pages if p["status"] == "draft"]
    title = f"Review chunk {chunk_index + 1}/{total_chunks} — {len(reviewed)} approved, {len(rejected)} rejected"
    sections = []
    for p in pages:
        verdict = {"reviewed": "APPROVED", "rejected": "REJECTED", "draft": "STILL DRAFT (unmarked!)"}[p["status"]]
        sections.append(f"### [{verdict}] {p['title']}  (staging id {p['id']})\n\n**Notes**: {p['review_notes'] or '(none)'}\n\n**Full page content:**\n\n{p['body']}")
    body = f"**Status: succeeded** — {len(reviewed)} approved, {len(rejected)} rejected" + (f", {len(still_draft)} left unmarked (bug -- flag this)" if still_draft else "") + ".\n\n---\n\n" + "\n\n---\n\n".join(sections)
    # Approved pages get auto-promoted right after this sweep (see
    # promote.py) and get their own "Pushed to Live" task once that
    # actually happens -- this task's own state reflects whether THIS
    # chunk needs a human to look at anything: unmarked pages are a bug
    # (still "Todo", same as before), any rejection needs a human
    # decision (new), a clean all-approved chunk needs nothing further.
    if still_draft:
        state = "Todo"
    elif rejected:
        state = "Needs Human Attention"
    else:
        state = "Done"
    _safe(_update, issue_id, title, body, state)


def _get_review_verdicts(page_ids):
    conn = get_live_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, title, page_type, status, review_notes, body FROM wiki_staging.wiki_pages WHERE id = ANY(%s) ORDER BY id",
                (page_ids,),
            )
            return cur.fetchall()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Pending-drafts board -- unchanged from the previous design, still valuable
# as a persistent, always-current, update-in-place summary of what's
# awaiting a human promote decision.
# ---------------------------------------------------------------------------

def _get_board_issue(board_key):
    conn = get_live_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT linear_issue_id, linear_issue_identifier FROM wiki_ingest_board_state WHERE board_key = %s",
                (board_key,),
            )
            return cur.fetchone()
    finally:
        conn.close()


def _set_board_issue(board_key, issue_id, identifier):
    conn = get_live_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO wiki_ingest_board_state (board_key, linear_issue_id, linear_issue_identifier)
                VALUES (%s, %s, %s)
                ON CONFLICT (board_key) DO UPDATE SET
                    linear_issue_id = EXCLUDED.linear_issue_id,
                    linear_issue_identifier = EXCLUDED.linear_issue_identifier,
                    updated_at = now()
                """,
                (board_key, issue_id, identifier),
            )
        conn.commit()
    finally:
        conn.close()


def _fetch_pending_drafts():
    conn = get_live_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, title, page_type, source_kind, cycle_id, live_page_id
                FROM wiki_staging.wiki_pages WHERE status = 'reviewed'
                ORDER BY source_kind, id
                """
            )
            reviewed = cur.fetchall()
            cur.execute(
                """
                SELECT id, title, page_type, source_kind, review_notes
                FROM wiki_staging.wiki_pages WHERE status = 'rejected'
                ORDER BY source_kind, id
                """
            )
            rejected = cur.fetchall()
    finally:
        conn.close()
    return reviewed, rejected


def _build_pending_board_body(reviewed, rejected):
    lines = [
        f"**Awaiting promotion approval: {len(reviewed)} draft(s)** — **Rejected: {len(rejected)}**",
        "",
        "This issue is updated IN PLACE every wiki-ingestion review sweep (not a new issue each "
        "run) -- always reflects the current full state of staging, not just one cycle's delta. "
        "See the per-cycle/per-sweep parent issues for the running changelog.",
        "",
        "Every row below is a `wiki_staging.wiki_pages` draft, DB-native. `staging_page_id` "
        "promotes via `wiki_ingestion/promote.py::promote_page()`.",
        "",
        "---",
        "",
        f"## Awaiting promotion approval ({len(reviewed)})",
        "",
    ]
    by_source = defaultdict(list)
    for r in reviewed:
        by_source[r["source_kind"]].append(r)
    for source_kind in sorted(by_source):
        items = by_source[source_kind]
        lines.append(f"### {source_kind} ({len(items)})")
        lines.append("")
        lines.append("| staging_page_id | title | page_type | action |")
        lines.append("|---|---|---|---|")
        for r in items:
            title = (r["title"] or "").replace("|", "\\|")
            action = "update" if r["live_page_id"] else "create"
            lines.append(f"| {r['id']} | {title} | {r['page_type']} | {action} |")
        lines.append("")

    lines.append(f"## Rejected ({len(rejected)})")
    lines.append("")
    if rejected:
        lines.append("| staging_page_id | title | source_kind | reason |")
        lines.append("|---|---|---|---|")
        for r in rejected:
            title = (r["title"] or "").replace("|", "\\|")
            reason = (r["review_notes"] or "").replace("\n", " ").replace("|", "\\|")[:300]
            lines.append(f"| {r['id']} | {title} | {r['source_kind']} | {reason} |")
    else:
        lines.append("(none)")

    return "\n".join(lines)


def report_pending_drafts_board():
    """Creates (first call ever) or updates in place (every call after
    that) a single persistent Linear issue listing EVERY currently-
    pending draft (awaiting promotion approval) and EVERY rejected draft
    with its reason. Never raises."""
    try:
        reviewed, rejected = _fetch_pending_drafts()
        body = _build_pending_board_body(reviewed, rejected)
        title = f"[BOARD] Wiki drafts pending approval ({len(reviewed)} awaiting, {len(rejected)} rejected)"

        existing = _get_board_issue("pending_drafts")
        if existing:
            _update_issue(existing["linear_issue_id"], title, body, state_name="Todo")
            logger.info("pending-drafts board updated: %s", existing["linear_issue_identifier"])
        else:
            result = _create_issue(title, body, state_name="Todo")
            issue = result["issue"]
            _set_board_issue("pending_drafts", issue["id"], issue["identifier"])
            logger.info("pending-drafts board created: %s", issue["identifier"])
    except Exception as e:
        logger.warning("pending-drafts board report failed (result unaffected): %s", e)


# ---------------------------------------------------------------------------
# Promotion -- 2026-08: now runs AUTOMATICALLY right after every review
# sweep (see run_review.py), no human gate for pages the review agent
# approved. Still a single summary issue per promotion batch, landed in
# the "Pushed to Live" state so it's a distinct, filterable record of
# what actually went live and when -- separate from the review chunk's
# own "approved" verdict (which happens a moment earlier, before
# promotion is attempted). Rejected pages are NEVER touched by this --
# they still require a human decision, tracked instead via the review
# chunk's "Needs Human Attention" state and the pending-drafts board.
# ---------------------------------------------------------------------------

def _build_promotion_report(result):
    lines = [
        f"**Wiki-ingestion promotion — {result['succeeded']}/{result['attempted']} promoted to live**",
        "",
        f"Newly resolved links: {result.get('newly_resolved_links', 0)}",
    ]
    if result.get("succeeded_list"):
        lines += ["", "| Live page id | Title | Action |", "|---|---|---|"]
        for s in result["succeeded_list"]:
            lines.append(f"| {s['live_page_id']} | {s['title']} | {s['action']} |")
    if result.get("failed"):
        lines += ["", "| Failed |", "|---|"]
        for f in result["failed"]:
            lines.append(f"| {f} |")

    title = f"Wiki-ingestion promotion — {result['succeeded']}/{result['attempted']} live"
    state = "Todo" if result.get("failed") else "Pushed to Live"
    return title, "\n".join(lines), state


def report_promotion(result):
    if not result.get("attempted"):
        logger.info("Promotion: nothing to promote -- skipping Linear report")
        return
    try:
        title, body, state = _build_promotion_report(result)
        _safe(_create, title, body, state)
    except Exception as e:
        logger.warning("Linear report failed (result unaffected): %s", e)
