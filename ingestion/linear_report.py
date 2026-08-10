"""Raw-ingestion Linear reporting on the EDB team (HANDOFF.md section 7,
item 6 -- originally deferred because the Linear workspace was capped at
one team, already used by wiki-agent's WIK board; EDB now exists for
real).

Deliberately lightweight, matching the original ask: one issue per
completed FULL SWEEP (every source, one run) -- NOT one per individual
webhook trigger (a single new Gmail message or Fireflies call would
otherwise flood the board with an issue apiece). ingest_log.py already
captures every trigger, webhook or sweep, granularly in Postgres; this is
the periodic human-glanceable summary layer on top, so it only fires for
the same runs ingest_log calls "sweep"-shaped: the 2-hourly cron timer
and the manual /trigger/manual endpoint, both of which run every source
via run_full_sweep().

Report content, per explicit request: how many rows landed, from which
source, exactly when, and which table(s) they landed in -- a markdown
table in the issue body, one row per source (or per account/client for
gmail/odoo's nested per-account/per-client results), not just a total
count.
"""
import logging
import os

import httpx

from ingestion.state import now_utc

logger = logging.getLogger("ingestion.linear_report")

LINEAR_API_URL = "https://api.linear.app/graphql"
LINEAR_EDB_API_KEY = os.environ.get("LINEAR_EDB_API_KEY")
LINEAR_EDB_TEAM_KEY = os.environ.get("LINEAR_EDB_TEAM_KEY", "EDB")

# Where each top-level sweep source's writes actually land -- static, since
# the fetchers' own return dicts (just counts) don't carry table names.
SOURCE_TABLES = {
    "gmail": "email_threads, email_messages, email_attachments",
    "zoho": "email_threads, email_messages, email_attachments",
    "fireflies": "call_transcripts, call_segments",
    "fathom": "call_transcripts, call_segments",
    "odoo": "implementation_tasks, implementation_task_events, implementation_task_attachments",
    "tickets": "tickets, ticket_events, ticket_attachments",
}

# Cap on how many item names to list per source in a report -- the fetchers'
# written_items lists (see below) can be arbitrarily long, don't want a
# single Linear issue body to balloon.
ITEMS_PER_SOURCE_LIMIT = 10

_team_id_cache = {}
_state_id_cache = {}

ISSUE_CREATE_MUTATION = """
mutation($teamId: String!, $title: String!, $description: String!, $stateId: String, $parentId: String) {
    issueCreate(input: {teamId: $teamId, title: $title, description: $description, stateId: $stateId, parentId: $parentId}) {
        success
        issue { id identifier url }
    }
}
"""

ISSUE_UPDATE_MUTATION = """
mutation($id: String!, $title: String!, $description: String!, $stateId: String) {
    issueUpdate(id: $id, input: {title: $title, description: $description, stateId: $stateId}) {
        success
        issue { id identifier url }
    }
}
"""

TEAMS_QUERY = """query($key: String!) { teams(filter: { key: { eq: $key } }) { nodes { id key } } }"""
STATES_QUERY = """query($teamKey: String!) { teams(filter: {key: {eq: $teamKey}}) { nodes { states { nodes { id name } } } } }"""


def _get_team_id():
    if "id" in _team_id_cache:
        return _team_id_cache["id"]
    resp = httpx.post(
        LINEAR_API_URL, json={"query": TEAMS_QUERY, "variables": {"key": LINEAR_EDB_TEAM_KEY}},
        headers={"Authorization": LINEAR_EDB_API_KEY}, timeout=15,
    )
    resp.raise_for_status()
    nodes = resp.json()["data"]["teams"]["nodes"]
    if not nodes:
        raise RuntimeError(f"no Linear team found with key={LINEAR_EDB_TEAM_KEY}")
    _team_id_cache["id"] = nodes[0]["id"]
    return _team_id_cache["id"]


def _get_state_id(name):
    """Looks up a workflow state id by name (e.g. 'Done', 'Todo') for the
    EDB team, cached after first call. Automation-report issues are
    completed events, not open work -- creating them with no stateId
    defaults to the team's initial state (Backlog), which is misleading
    (they pile up looking like unstarted work) and not what's wanted."""
    if name in _state_id_cache:
        return _state_id_cache[name]
    resp = httpx.post(
        LINEAR_API_URL, json={"query": STATES_QUERY, "variables": {"teamKey": LINEAR_EDB_TEAM_KEY}},
        headers={"Authorization": LINEAR_EDB_API_KEY}, timeout=15,
    )
    resp.raise_for_status()
    teams = resp.json()["data"]["teams"]["nodes"]
    if not teams:
        raise RuntimeError(f"no Linear team found with key={LINEAR_EDB_TEAM_KEY}")
    for state in teams[0]["states"]["nodes"]:
        _state_id_cache[state["name"]] = state["id"]
    return _state_id_cache.get(name)


def _create_issue(title, description, state_name=None, parent_id=None):
    team_id = _get_team_id()
    state_id = _get_state_id(state_name) if state_name else None
    resp = httpx.post(
        LINEAR_API_URL,
        json={"query": ISSUE_CREATE_MUTATION, "variables": {"teamId": team_id, "title": title, "description": description, "stateId": state_id, "parentId": parent_id}},
        headers={"Authorization": LINEAR_EDB_API_KEY},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("errors"):
        raise RuntimeError(f"Linear issueCreate errors: {data['errors']}")
    return data["data"]["issueCreate"]


def _update_issue(issue_id, title, description, state_name=None):
    """Updates an existing issue in place -- for persistent 'board' issues
    that should stay as ONE issue reflecting current state, not spawn a
    new issue every time something changes (e.g. the pending-drafts
    board, refreshed every wiki-ingestion cycle)."""
    state_id = _get_state_id(state_name) if state_name else None
    resp = httpx.post(
        LINEAR_API_URL,
        json={"query": ISSUE_UPDATE_MUTATION, "variables": {"id": issue_id, "title": title, "description": description, "stateId": state_id}},
        headers={"Authorization": LINEAR_EDB_API_KEY},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("errors"):
        raise RuntimeError(f"Linear issueUpdate errors: {data['errors']}")
    return data["data"]["issueUpdate"]


def _items_for(detail):
    """detail: the fetcher's own per-source/per-account result dict, which
    now carries a written_items list of exactly what it wrote (real names,
    not a guess). Returns (items, truncated).

    This used to run a fresh DB query filtered by "updated_at within the
    last 20 minutes" -- found live to be wrong: after a bulk operation
    touches updated_at on tens of thousands of unrelated rows (a historical
    backfill, a data-fix reload), EVERY report for the next 20+ minutes
    picks up that stale pool instead of what this run actually wrote,
    since the query has no way to distinguish "recently touched for any
    reason" from "written by this specific fetcher call." Reading
    written_items directly is exact by construction -- no time window,
    no guessing, no dependency on nothing else having touched the table
    recently.

    detail isn't always a dict -- odoo's per-client result is a bare int
    or None (see _rows), and it has no written_items concept at all (full
    refresh every run, not incremental writes -- see the note appended in
    _format_body)."""
    if not isinstance(detail, dict):
        return [], False
    items = detail.get("written_items") or []
    truncated = len(items) > ITEMS_PER_SOURCE_LIMIT
    return items[:ITEMS_PER_SOURCE_LIMIT], truncated


def _rows(result):
    """Flattens run_full_sweep()'s {source: counts | {subkey: counts}}
    shape into (source, sub, label, table, written, error, detail) rows --
    one row per source, or per account/client for gmail/odoo's nested
    results. source/sub are kept (not just the display label) so the
    caller can look up real item names via _items_for."""
    rows = []
    for source, val in result.items():
        table = SOURCE_TABLES.get(source, "?")
        if isinstance(val, dict) and "written" in val:
            rows.append((source, None, source, table, val.get("written") or 0, val.get("error") or 0, val))
        elif isinstance(val, dict):
            for sub, subval in val.items():
                label = f"{source} ({sub})"
                if isinstance(subval, dict) and "written" in subval:
                    rows.append((source, sub, label, table, subval.get("written") or 0, subval.get("error") or 0, subval))
                elif isinstance(subval, (int, float)):
                    # odoo's per-client result is a bare row count, not a {"written":...}
                    # dict (see odoo_fetcher.process_client) -- a real number here means success.
                    rows.append((source, sub, label, table, subval, 0, subval))
                elif subval is None:
                    # odoo_fetcher.process_all sets this on a real per-client exception.
                    rows.append((source, sub, label, table, 0, 1, {"error": "exception -- see server logs"}))
                else:
                    rows.append((source, sub, label, table, 0, 1, {"error": str(subval)}))
        elif isinstance(val, (int, float)):
            rows.append((source, None, source, table, val, 0, val))
        else:
            rows.append((source, None, source, table, 0, 1, {"error": str(val)}))
    return rows


def _format_body(result, run_at):
    rows = _rows(result)
    total_written = sum(r[4] for r in rows)
    total_errors = sum(1 for r in rows if r[5])

    lines = [
        f"**Raw ingestion run — {run_at.strftime('%Y-%m-%d %H:%M:%S UTC')}**",
        "",
        "| Source | Table(s) | New rows | Errors |",
        "|---|---|---|---|",
    ]
    for source, sub, label, table, written, error, _ in rows:
        lines.append(f"| {label} | {table} | {written} | {error or 0} |")
    lines += ["", f"**Total new rows: {total_written}**" + (f"  ⚠️ {total_errors} source(s) with errors" if total_errors else "")]

    item_sections = []
    for source, sub, label, table, written, error, detail in rows:
        if not written:
            continue
        items, truncated = _items_for(detail)
        if items:
            item_sections.append(f"**{label}** ({written} written):\n" + "\n".join(f"- {i}" for i in items) + (f"\n- _...and {written - ITEMS_PER_SOURCE_LIMIT} more_" if truncated else ""))
    if item_sections:
        lines += ["", "---", "", "**What actually landed:**", ""] + item_sections
    odoo_written = sum(r[4] for r in rows if r[0] == "odoo")
    if odoo_written:
        lines += ["", "_odoo does a full client-task refresh every run (not incremental), so individual \"new\" items aren't listed here -- use list_implementation_tasks/get_implementation_task to browse._"]
    return "\n".join(lines)


def _title(result, run_at):
    rows = _rows(result)
    total_written = sum(r[4] for r in rows)
    has_errors = any(r[5] for r in rows)
    suffix = " (errors)" if has_errors else ""
    return f"Raw ingestion run — {run_at.strftime('%Y-%m-%d %H:%M UTC')} — {total_written} new row(s){suffix}"


def report_full_sweep(result):
    """Legacy single-issue report -- kept for any external caller still
    using it, but run_full_sweep() itself now uses the granular
    start_sweep_parent/start_source_task flow below (2026-08 redesign,
    matching wiki_ingestion's per-sub-agent-task model: one parent issue
    per sweep, one child task per source, created before that source
    runs and updated with its real outcome after -- not one aggregate
    issue after the fact)."""
    if not LINEAR_EDB_API_KEY:
        logger.warning("LINEAR_EDB_API_KEY not set -- skipping Linear report")
        return
    try:
        run_at = now_utc()
        title = _title(result, run_at)
        description = _format_body(result, run_at)
        has_errors = any(r[5] for r in _rows(result))
        created = _create_issue(title, description, state_name="Todo" if has_errors else "Done")
        if not created.get("success"):
            logger.warning("Linear issueCreate returned success=false: %s", created)
        else:
            logger.info("Linear report created: %s", created["issue"]["identifier"])
    except Exception as e:
        logger.warning("Linear report failed (ingestion result unaffected): %s", e)


def start_sweep_parent():
    """Creates the parent issue for a sweep at the moment it starts, before
    any source has run -- so a stuck/slow sweep is visible while in
    flight. Returns the Linear issue id, or None (never raises)."""
    if not LINEAR_EDB_API_KEY:
        return None
    try:
        run_at = now_utc()
        title = f"Raw ingestion sweep — {run_at.strftime('%Y-%m-%d %H:%M UTC')} — running"
        body = f"Started {run_at.strftime('%Y-%m-%d %H:%M:%S UTC')}. Each source below runs as its own sub-task."
        created = _create_issue(title, body, state_name="Todo")
        if not created.get("success"):
            logger.warning("Linear issueCreate returned success=false: %s", created)
            return None
        return created["issue"]["id"]
    except Exception as e:
        logger.warning("Linear sweep-parent create failed: %s", e)
        return None


def finish_sweep_parent(parent_issue_id, result, run_at):
    if not parent_issue_id:
        return
    try:
        rows = _rows(result)
        total_written = sum(r[4] for r in rows)
        has_errors = any(r[5] for r in rows)
        title = f"Raw ingestion sweep — {run_at.strftime('%Y-%m-%d %H:%M UTC')} — {total_written} new row(s)" + (" (errors)" if has_errors else "")
        body = (
            f"**Total new rows: {total_written}**" + (f"  ⚠️ some source(s) had errors" if has_errors else "") +
            "\n\nOpen the sub-issues below for each source's exact rows/tables."
        )
        _update_issue(parent_issue_id, title, body, state_name="Todo" if has_errors else "Done")
    except Exception as e:
        logger.warning("Linear sweep-parent finish failed: %s", e)


def start_source_task(parent_issue_id, name):
    if not parent_issue_id:
        return None
    try:
        title = f"Sweep · {name} — running"
        table = SOURCE_TABLES.get(name, "?")
        body = f"**Phase**: raw ingestion sweep\n**Source**: {name}\n**Target table(s)**: {table}\n"
        created = _create_issue(title, body, state_name="Todo", parent_id=parent_issue_id)
        if not created.get("success"):
            logger.warning("Linear issueCreate returned success=false: %s", created)
            return None
        return created["issue"]["id"]
    except Exception as e:
        logger.warning("Linear source-task create failed for %s: %s", name, e)
        return None


def finish_source_task(issue_id, name, detail):
    """detail: exactly what that source's fetch function returned (a
    {"written":N,"error":N,"written_items":[...]} dict, a nested
    per-account/per-client dict, a bare int, or None on exception --
    same shapes _rows() already knows how to flatten)."""
    if not issue_id:
        return
    try:
        sub_rows = _rows({name: detail})
        written = sum(r[4] for r in sub_rows)
        errors = sum(1 for r in sub_rows if r[5])

        lines = [f"**Source**: {name}", ""]
        if len(sub_rows) > 1:
            lines += ["| Account/client | Table(s) | New rows | Errors |", "|---|---|---|---|"]
            for _, _, label, table, w, e, _ in sub_rows:
                lines.append(f"| {label} | {table} | {w} | {e or 0} |")
            lines.append("")

        for _, _, label, table, w, e, d in sub_rows:
            if not w:
                continue
            items, truncated = _items_for(d)
            if items:
                lines.append(f"**{label}** ({w} written):")
                lines += [f"- {i}" for i in items]
                if truncated:
                    lines.append(f"- _...and {w - ITEMS_PER_SOURCE_LIMIT} more_")
                lines.append("")

        if name == "odoo" and written:
            lines.append("_odoo does a full client-task refresh every run (not incremental), so individual \"new\" items aren't listed -- use list_implementation_tasks/get_implementation_task to browse._")

        title = f"Sweep · {name} — {written} new row(s)" + (" (errors)" if errors else "")
        body = "\n".join(lines) if len(lines) > 2 else f"**Source**: {name}\n\n{written} new row(s), {errors} error(s)."
        _update_issue(issue_id, title, body, state_name="Todo" if errors else "Done")
    except Exception as e:
        logger.warning("Linear source-task finish failed for %s: %s", name, e)
