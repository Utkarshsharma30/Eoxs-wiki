"""Raw-ingestion run observability: writes one row per completed run
(webhook-triggered single-source or cron/manual full-sweep) into the
existing ingest_log table, dual-written to live+staging like everything
else. Replaces the originally-planned Linear EDB reporting -- the Linear
workspace's plan caps it at 1 team, already used by wiki-agent's WIK
board, so a second team isn't available. Detailed per-request/per-fetcher
logs still go through standard Python logging -> journalctl once this
runs as a systemd service; ingest_log is the SQL-queryable summary layer
on top of that, mirroring the file-based system's log.md replica table.
"""
import logging

from ingestion.db import dual_write
from ingestion.state import now_utc

logger = logging.getLogger("ingestion.ingest_log")


def _is_error_dict(val):
    """True only for the {"error": "message"} shape a bare except-clause
    produces -- NOT a fetcher's normal counts dict, which always has an
    "error" key too (a count, usually 0)."""
    return isinstance(val, dict) and set(val.keys()) == {"error"}


def _format_summary_line(source, result):
    if _is_error_dict(result):
        return f"- {source}: ERROR — {result['error']}"
    if isinstance(result, dict) and "written" in result:
        return f"- {source}: {result}"
    if isinstance(result, dict):
        # gmail (per-account) / odoo (per-client) nested results
        parts = []
        for key, val in result.items():
            if val is None:
                parts.append(f"{key}: ERROR")
            elif _is_error_dict(val):
                parts.append(f"{key}: ERROR — {val['error']}")
            else:
                parts.append(f"{key}: {val}")
        return f"- {source}: " + "; ".join(parts)
    return f"- {source}: {result}"


def _total_written(summary):
    total = 0
    for result in summary.values():
        if not isinstance(result, dict):
            continue
        if "written" in result:
            total += result["written"] or 0
            continue
        for val in result.values():
            if isinstance(val, dict) and "written" in val:
                total += val["written"] or 0
            elif isinstance(val, int):
                total += val
    return total


def _has_errors(summary):
    for result in summary.values():
        if not isinstance(result, dict):
            continue
        if _is_error_dict(result):
            return True
        if result.get("error"):  # a fetcher's own error COUNT, e.g. {"error": 2}
            return True
        for val in result.values():
            if val is None:
                return True
            if _is_error_dict(val) or (isinstance(val, dict) and val.get("error")):
                return True
    return False


def summarize(result):
    """Returns (description, raw_entry) for either shape a trigger can
    produce: a single fetcher's counts dict (webhook-triggered single
    source), or a multi-source dict (gmail's per-account results, odoo's
    per-client results, or server.run_full_sweep()'s top-level summary)."""
    if isinstance(result, dict) and "written" in result:
        written = result.get("written") or 0
        has_err = bool(result.get("error"))
        description = f"{written} new row(s)" + (" (errors)" if has_err else "")
        return description, str(result)

    if isinstance(result, dict):
        written = _total_written(result)
        has_err = _has_errors(result)
        description = f"{written} new row(s)" + (" (errors)" if has_err else "")
        raw_entry = "\n".join(_format_summary_line(src, val) for src, val in result.items())
        return description, raw_entry

    return str(result), str(result)


def write_run_entry(conn, *, log_date, operation, description, raw_entry):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO ingest_log (log_date, operation, description, raw_entry) VALUES (%s,%s,%s,%s)",
            (log_date, operation, description, raw_entry),
        )
    conn.commit()


def log_run(trigger_source, result):
    """Never raises -- an ingest_log write failure should never fail or
    mask an otherwise-successful ingestion run."""
    try:
        description_summary, raw_entry = summarize(result)
        dual_write(
            write_run_entry,
            log_date=now_utc().date(), operation="ingest",
            description=f"{trigger_source}: {description_summary}",
            raw_entry=raw_entry,
        )
    except Exception as e:
        logger.warning("ingest_log write failed (ingestion result unaffected): %s", e)
