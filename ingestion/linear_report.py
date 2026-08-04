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

from ingestion.db import get_live_conn
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

# How many hits ago (report_full_sweep's window_minutes) counts as "part of
# this run" for listing actual item names -- generous vs. observed sweep
# runtimes (typically 1-3 min) without needing to thread an exact start
# timestamp through every call site.
ITEMS_PER_SOURCE_LIMIT = 10

_team_id_cache = {}
_state_id_cache = {}

ISSUE_CREATE_MUTATION = """
mutation($teamId: String!, $title: String!, $description: String!, $stateId: String) {
    issueCreate(input: {teamId: $teamId, title: $title, description: $description, stateId: $stateId}) {
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


def _create_issue(title, description, state_name=None):
    team_id = _get_team_id()
    state_id = _get_state_id(state_name) if state_name else None
    resp = httpx.post(
        LINEAR_API_URL,
        json={"query": ISSUE_CREATE_MUTATION, "variables": {"teamId": team_id, "title": title, "description": description, "stateId": state_id}},
        headers={"Authorization": LINEAR_EDB_API_KEY},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("errors"):
        raise RuntimeError(f"Linear issueCreate errors: {data['errors']}")
    return data["data"]["issueCreate"]


def _recent_items(table, extra_where, params, name_col):
    """Real item names/subjects for 'what actually landed', queried fresh
    from the DB rather than carried through the fetchers' return values
    (which are just counts) -- avoids changing every fetcher's contract."""
    conn = get_live_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {name_col} AS name FROM {table} WHERE updated_at >= now() - interval '20 minutes' {extra_where} "
                f"ORDER BY updated_at DESC LIMIT {ITEMS_PER_SOURCE_LIMIT + 1}",
                params,
            )
            rows = [r["name"] for r in cur.fetchall()]
        truncated = len(rows) > ITEMS_PER_SOURCE_LIMIT
        return rows[:ITEMS_PER_SOURCE_LIMIT], truncated
    finally:
        conn.close()


def _items_for(source, sub):
    """sub: the per-account (gmail) / per-source (fireflies/fathom) / plain
    (zoho/tickets) key, or None for odoo (see below). Returns
    (items, truncated) or (None, False) if this source/sub has no
    meaningful "recent items" concept."""
    if source == "gmail":
        return _recent_items("email_threads", "AND source_account = %s", (sub,), "subject")
    if source == "zoho":
        return _recent_items("email_threads", "AND source_account = 'support_zoho'", (), "subject")
    if source in ("fireflies", "fathom"):
        return _recent_items("call_transcripts", "AND source = %s", (source,), "meeting_title")
    if source == "tickets":
        return _recent_items("tickets", "", (), "ticket_number || ': ' || subject")
    return None, False  # odoo: full client refresh every run, "new items" isn't a meaningful concept here


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
    for source, sub, label, table, written, error, _ in rows:
        if not written:
            continue
        items, truncated = _items_for(source, sub)
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
    """Never raises -- a Linear reporting failure should never fail or
    mask an otherwise-successful ingestion run (same discipline as
    ingest_log.log_run)."""
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
