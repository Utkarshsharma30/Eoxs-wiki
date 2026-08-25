"""One-off manual rerun of the 4 AskCruz asset rows (ids 16-19) that were
silently skipped by cycle 93's assets batch due to the _row_summary bug
fixed in run_agent.py (assets fell through to the email-thread format,
so the sub-agent saw blank summaries and drafted nothing -- see EDB-2241).

Bypasses the normal cursor/wiki_ingest_seen dedup path on purpose: those
4 rows' updated_at and content_hash haven't changed since cycle 93, so a
normal run_detection() call would just skip them again as unchanged. This
script fetches them directly and drives run_agent() the same way run_cycle
does for a real batch, so the run still gets a normal wiki_ingest_cycles/
wiki_ingest_batches/Linear audit trail -- just scoped to exactly these rows.

Not meant to be run again after this -- a normal cycle picks up assets
correctly from here on now that the bug is fixed.
"""
import json

from ingestion.db import get_live_conn
from ingestion.state import now_utc
from wiki_ingestion.detect import candidates_assets
from wiki_ingestion.run_agent import run_agent
from wiki_ingestion.linear_report import start_cycle_parent, finish_cycle_parent, start_batch_task, finish_batch_task

ASSET_IDS = (16, 17, 18, 19)


def _start_cycle():
    conn = get_live_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO wiki_ingest_cycles (status) VALUES ('running') RETURNING id")
            cycle_id = cur.fetchone()["id"]
        conn.commit()
        return cycle_id
    finally:
        conn.close()


def _set_cycle_linear_parent(cycle_id, issue_id):
    if not issue_id:
        return
    conn = get_live_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE wiki_ingest_cycles SET linear_parent_issue_id = %s WHERE id = %s", (issue_id, cycle_id))
        conn.commit()
    finally:
        conn.close()


def _finish_cycle(cycle_id, status, summary):
    conn = get_live_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE wiki_ingest_cycles SET status = %s, summary = %s, finished_at = now() WHERE id = %s",
                (status, json.dumps(summary), cycle_id),
            )
        conn.commit()
    finally:
        conn.close()


def _start_batch(cycle_id, source_kind, row_count, linear_issue_id=None):
    conn = get_live_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO wiki_ingest_batches (cycle_id, source_kind, row_count, linear_issue_id) VALUES (%s, %s, %s, %s) RETURNING id",
                (cycle_id, source_kind, row_count, linear_issue_id),
            )
            batch_id = cur.fetchone()["id"]
        conn.commit()
        return batch_id
    finally:
        conn.close()


def _finish_batch(batch_id, status, error=None):
    conn = get_live_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE wiki_ingest_batches SET status = %s, error = %s, finished_at = now() WHERE id = %s",
                (status, error, batch_id),
            )
        conn.commit()
    finally:
        conn.close()


def main():
    all_assets = {r["id"]: r for r in candidates_assets(since=None)}
    rows = [all_assets[i] for i in ASSET_IDS if i in all_assets]
    missing = [i for i in ASSET_IDS if i not in all_assets]
    if missing:
        print(f"WARNING: asset ids not found: {missing}")
    if not rows:
        print("No target rows found, aborting.")
        return

    cycle_id = _start_cycle()
    print(f"cycle_id={cycle_id}")

    parent_issue_id = start_cycle_parent(cycle_id)
    _set_cycle_linear_parent(cycle_id, parent_issue_id)

    task_issue_id = start_batch_task(parent_issue_id, cycle_id, "assets", 1, 1, rows)
    batch_id = _start_batch(cycle_id, "assets", len(rows), task_issue_id)
    print(f"batch_id={batch_id}, running agent on {len(rows)} row(s): {[r['slug'] for r in rows]}")

    since_ts = now_utc()
    result = run_agent(cycle_id, "assets", rows, timeout_seconds=1200)
    finish_batch_task(task_issue_id, cycle_id, "assets", 1, 1, result, since_ts)

    if result["ok"]:
        _finish_batch(batch_id, "done")
        batches = [{"source_kind": "assets", "row_count": len(rows), "status": "done"}]
        summary = {"status": "done", "batches": batches, "batches_total": 1, "batches_failed": 0, "partitions_total": 1}
        _finish_cycle(cycle_id, "done", summary)
        finish_cycle_parent(parent_issue_id, cycle_id, summary)
        print("DONE. stdout:")
        print(result.get("stdout", ""))
    else:
        error = result.get("stderr") or result.get("stdout") or "unknown error"
        _finish_batch(batch_id, "ingest_failed", error)
        batches = [{"source_kind": "assets", "row_count": len(rows), "status": "ingest_failed", "error": error}]
        summary = {"status": "done", "batches": batches, "batches_total": 1, "batches_failed": 1, "partitions_total": 1}
        _finish_cycle(cycle_id, "done", summary)
        finish_cycle_parent(parent_issue_id, cycle_id, summary)
        print("FAILED:")
        print(error)


if __name__ == "__main__":
    main()
