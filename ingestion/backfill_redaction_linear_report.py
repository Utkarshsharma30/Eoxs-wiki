"""One-time backfill: reports every mcp_redaction_log row that predates
ingestion/redaction_linear_report.py's existence (2026-08-07 through
today) into the same parent/child Linear structure that report will use
going forward, grouped into the real 2-hour windows those events actually
occurred in -- so the historical board looks the same shape it would have
if this reporting had been running from day one, rather than one giant
flat dump.

Not idempotent by design -- re-running this after a first successful run
would duplicate every historical issue, since it does not use
redaction_report_cursor (that cursor is reserved for the ONGOING
2-hourly report, see redaction_linear_report.py's module docstring). This
sets the cursor to the highest id it backfilled when done, so the next
real 2-hourly sweep picks up cleanly from there and never re-reports
anything this script already covered.

Run once, by hand: python -m ingestion.backfill_redaction_linear_report
"""
import logging

from ingestion.db import get_live_conn
from ingestion.redaction_linear_report import _rollup_title, _rollup_body, _child_title, _child_body, _safe, _create, _set_cursor
from ingestion.state import now_utc

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("ingestion.backfill_redaction_linear_report")


def _fetch_all(conn):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, occurred_at, clearance_name, tool_name, redacted_snippets "
            "FROM mcp_redaction_log ORDER BY id"
        )
        return cur.fetchall()


def _window_key(occurred_at):
    """Real 2-hour window the event fell in, e.g. 2026-08-31 18:00 UTC."""
    hour = (occurred_at.hour // 2) * 2
    return occurred_at.replace(hour=hour, minute=0, second=0, microsecond=0)


def _group_by_window(events):
    windows = {}
    for e in events:
        key = _window_key(e["occurred_at"])
        windows.setdefault(key, []).append(e)
    return dict(sorted(windows.items()))


def main():
    conn = get_live_conn()
    try:
        events = _fetch_all(conn)
        if not events:
            logger.info("mcp_redaction_log is empty -- nothing to backfill")
            return
        windows = _group_by_window(events)
        logger.info("backfilling %d events across %d historical 2-hour windows", len(events), len(windows))

        created_parents = 0
        created_children = 0
        for window_start, window_events in windows.items():
            window_label = f"{window_start.strftime('%Y-%m-%d %H:%M UTC')} (backfill)"
            title = _rollup_title(window_events, window_label)
            body = _rollup_body(window_events, window_label) + (
                "\n\n*Backfilled retroactively -- these events predate the redaction Linear "
                "reporting itself (added 2026-09-02); grouped into the real 2-hour window each "
                "occurred in, matching how this would look if reporting had been running from day one.*"
            )
            parent_id = _safe(_create, title, body, "Done")
            if not parent_id:
                logger.warning("failed to create backfill parent for window=%s -- skipping its %d event(s)",
                                window_start, len(window_events))
                continue
            created_parents += 1
            for event in window_events:
                child_id = _safe(_create, _child_title(event), _child_body(event), "Done", parent_id)
                if child_id:
                    created_children += 1

        logger.info("backfill done: %d parent issue(s), %d child issue(s) created", created_parents, created_children)

        highest_id = events[-1]["id"]
        _set_cursor(conn, highest_id)
        logger.info("redaction_report_cursor advanced to id=%d -- next 2-hourly sweep starts clean from here", highest_id)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
