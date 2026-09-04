"""Linear reporting for mcp_server/redaction.py's query-time redaction
safety net (mcp_redaction_log, schema/023) -- surfaces on the EDB team so
a human can review, per event: which MCP identity the call came through,
which tool it ran, and exactly what content got stripped before the
response reached that caller.

Same parent/child shape as wiki_ingestion/linear_report.py's cycle
reporting, for the same reason: a flat "one issue per redaction" would
flood the EDB board (up to ~50+ events in a single 2-hour window on busy
days -- confirmed live, see docs/backend-server.md §5.4-adjacent history).
Instead:

  - One PARENT issue per 2-hourly reporting cycle (same cadence as
    ingestion/linear_report.py's sweep parent, called from the same
    run_full_sweep()), titled with a scannable rollup -- total count, plus
    a per-identity/per-tool breakdown -- so the board itself is reviewable
    without opening anything.
  - One Linear SUB-ISSUE (via parentId) per individual redaction event
    nested under that parent -- full per-event detail (which MCP, which
    tool, the exact redacted spans, when) preserved and drillable, without
    each one showing up as its own top-level board item.
  - A cycle with zero new redaction events since the last report creates
    NO parent issue at all -- an empty/quiet cycle isn't worth a Linear
    issue, same convention as every other reporter in this codebase.

Reuses the low-level Linear GraphQL client (_create_issue/_update_issue)
from ingestion/linear_report.py rather than duplicating it -- same EDB
team, same API key.

Progress cursor: redaction_report_cursor (schema/036) tracks the highest
mcp_redaction_log.id already reported, so each run only picks up genuinely
new rows since the last one, not the whole table again.
"""
import logging
from collections import defaultdict

from ingestion.db import get_live_conn
from ingestion.linear_report import _create_issue, _update_issue
from ingestion.state import now_utc

logger = logging.getLogger("ingestion.redaction_linear_report")


def _safe(fn, *args, **kwargs):
    """Never raises -- a Linear reporting failure should never fail or mask
    an otherwise-successful sweep, matching every other reporter here."""
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        logger.warning("redaction Linear report step failed (result unaffected): %s", e)
        return None


def _create(title, body, state_name="Done", parent_id=None):
    created = _create_issue(title, body, state_name=state_name, parent_id=parent_id)
    if not created or not created.get("success"):
        logger.warning("Linear issueCreate returned success=false: %s", created)
        return None
    return created["issue"]["id"]


def _get_cursor(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT last_reported_id FROM redaction_report_cursor WHERE id = 'singleton'")
        row = cur.fetchone()
        return row["last_reported_id"] if row else 0


def _set_cursor(conn, last_reported_id):
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE redaction_report_cursor SET last_reported_id = %s, updated_at = now() WHERE id = 'singleton'",
            (last_reported_id,),
        )
    conn.commit()


def _fetch_new_events(conn, since_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, occurred_at, clearance_name, tool_name, redacted_snippets "
            "FROM mcp_redaction_log WHERE id > %s ORDER BY id",
            (since_id,),
        )
        return cur.fetchall()


def _rollup_title(events, window_label):
    by_identity = defaultdict(lambda: defaultdict(int))
    for e in events:
        by_identity[e["clearance_name"]][e["tool_name"]] += 1
    parts = []
    for identity in sorted(by_identity):
        tool_counts = by_identity[identity]
        total = sum(tool_counts.values())
        top_tools = ", ".join(f"{t} {n}" for t, n in sorted(tool_counts.items(), key=lambda kv: -kv[1])[:3])
        parts.append(f"{identity} {total} ({top_tools})")
    return f"[REDACTION] {window_label}: {len(events)} event(s) — " + " · ".join(parts)


def _rollup_body(events, window_label):
    by_identity = defaultdict(lambda: defaultdict(int))
    for e in events:
        by_identity[e["clearance_name"]][e["tool_name"]] += 1
    lines = [
        f"**{len(events)} redaction event(s), {window_label}**",
        "",
        "Query-time redaction safety net (mcp_server/redaction.py) stripped content from a tool "
        "response before it reached a non-full-clearance caller. Each event below is a Linear "
        "sub-issue (see the sub-issues panel) with the exact tool and redacted text — a row "
        "appearing here means the original access-tier classification of that content was wrong; "
        "use it to find and fix the source, not just to confirm the safety net caught it.",
        "",
        "| MCP identity | Tool | Count |",
        "|---|---|---|",
    ]
    for identity in sorted(by_identity):
        for tool, count in sorted(by_identity[identity].items(), key=lambda kv: -kv[1]):
            lines.append(f"| {identity} | {tool} | {count} |")
    return "\n".join(lines)


def _child_title(event):
    ts = event["occurred_at"].strftime("%Y-%m-%d %H:%M UTC")
    return f"Redaction: {event['clearance_name']}/{event['tool_name']} @ {ts}"


def _child_body(event):
    snippets = event["redacted_snippets"] or []
    lines = [
        f"**MCP identity:** {event['clearance_name']}",
        f"**Tool:** {event['tool_name']}",
        f"**Occurred:** {event['occurred_at'].strftime('%Y-%m-%d %H:%M:%S UTC')}",
        f"**Redacted content ({len(snippets)} span(s)):**",
        "",
    ]
    for s in snippets:
        lines.append(f"- `{s}`")
    return "\n".join(lines)


def report_new_redaction_events(window_label=None):
    """Call once per 2-hourly sweep (see ingestion/server.py's
    run_full_sweep()). Reports every mcp_redaction_log row written since
    the last call as one parent + N child sub-issues; does nothing (no
    parent created) if there are no new rows. Never raises."""
    conn = get_live_conn()
    try:
        since_id = _get_cursor(conn)
        events = _fetch_new_events(conn, since_id)
        if not events:
            logger.info("no new redaction events since id=%d — nothing to report", since_id)
            return
        window_label = window_label or now_utc().strftime("%Y-%m-%d %H:%M UTC")

        parent_id = _safe(_create, _rollup_title(events, window_label), _rollup_body(events, window_label), "Done")
        if parent_id:
            for event in events:
                _safe(_create, _child_title(event), _child_body(event), "Done", parent_id)
        else:
            logger.warning(
                "redaction parent issue creation failed — %d event(s) NOT reported to Linear this cycle, "
                "will be retried next cycle (cursor not advanced)", len(events),
            )
            return

        _set_cursor(conn, events[-1]["id"])
        logger.info("reported %d new redaction event(s) to Linear, cursor advanced to id=%d", len(events), events[-1]["id"])
    finally:
        conn.close()
