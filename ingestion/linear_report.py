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

_team_id_cache = {}

ISSUE_CREATE_MUTATION = """
mutation($teamId: String!, $title: String!, $description: String!) {
    issueCreate(input: {teamId: $teamId, title: $title, description: $description}) {
        success
        issue { id identifier url }
    }
}
"""

TEAMS_QUERY = """query($key: String!) { teams(filter: { key: { eq: $key } }) { nodes { id key } } }"""


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


def _create_issue(title, description):
    team_id = _get_team_id()
    resp = httpx.post(
        LINEAR_API_URL,
        json={"query": ISSUE_CREATE_MUTATION, "variables": {"teamId": team_id, "title": title, "description": description}},
        headers={"Authorization": LINEAR_EDB_API_KEY},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("errors"):
        raise RuntimeError(f"Linear issueCreate errors: {data['errors']}")
    return data["data"]["issueCreate"]


def _rows(result):
    """Flattens run_full_sweep()'s {source: counts | {subkey: counts}}
    shape into (label, table, written, error, detail) rows -- one row per
    source, or per account/client for gmail/odoo's nested results."""
    rows = []
    for source, val in result.items():
        table = SOURCE_TABLES.get(source, "?")
        if isinstance(val, dict) and "written" in val:
            rows.append((source, table, val.get("written") or 0, val.get("error") or 0, val))
        elif isinstance(val, dict):
            for sub, subval in val.items():
                label = f"{source} ({sub})"
                if isinstance(subval, dict) and "written" in subval:
                    rows.append((label, table, subval.get("written") or 0, subval.get("error") or 0, subval))
                elif isinstance(subval, (int, float)):
                    # odoo's per-client result is a bare row count, not a {"written":...}
                    # dict (see odoo_fetcher.process_client) -- a real number here means success.
                    rows.append((label, table, subval, 0, subval))
                elif subval is None:
                    # odoo_fetcher.process_all sets this on a real per-client exception.
                    rows.append((label, table, 0, 1, {"error": "exception -- see server logs"}))
                else:
                    rows.append((label, table, 0, 1, {"error": str(subval)}))
        elif isinstance(val, (int, float)):
            rows.append((source, table, val, 0, val))
        else:
            rows.append((source, table, 0, 1, {"error": str(val)}))
    return rows


def _format_body(result, run_at):
    rows = _rows(result)
    total_written = sum(r[2] for r in rows)
    total_errors = sum(1 for r in rows if r[3])

    lines = [
        f"**Raw ingestion run — {run_at.strftime('%Y-%m-%d %H:%M:%S UTC')}**",
        "",
        "| Source | Table(s) | New rows | Errors |",
        "|---|---|---|---|",
    ]
    for label, table, written, error, _ in rows:
        err_display = error if error else 0
        lines.append(f"| {label} | {table} | {written} | {err_display} |")
    lines += ["", f"**Total new rows: {total_written}**" + (f"  ⚠️ {total_errors} source(s) with errors" if total_errors else "")]
    return "\n".join(lines)


def _title(result, run_at):
    rows = _rows(result)
    total_written = sum(r[2] for r in rows)
    has_errors = any(r[3] for r in rows)
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
        created = _create_issue(title, description)
        if not created.get("success"):
            logger.warning("Linear issueCreate returned success=false: %s", created)
        else:
            logger.info("Linear report created: %s", created["issue"]["identifier"])
    except Exception as e:
        logger.warning("Linear report failed (ingestion result unaffected): %s", e)
