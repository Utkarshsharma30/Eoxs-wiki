"""Phase 3: cycle driver -- fans detection out across every category
partition, running one headless sub-agent (run_agent.py) per chunk of
each partition and recording per-batch/per-cycle outcomes in
wiki_ingest_cycles/wiki_ingest_batches.

Large partitions are split into fixed-size chunks (CHUNK_SIZE rows each),
each chunk its own sub-agent call: a single `claude -p` invocation asked
to synthesize 200+ raw rows (real partition sizes seen on the initial
backfill: client_discount-pipe-steel=233, client_eastern-states-steel=225,
client_sabre-alloys=200) produces shallow, unfocused output, so a
source_kind with N rows becomes ceil(N/CHUNK_SIZE) separate batches under
the same cycle rather than one.

Batches run SEQUENTIALLY, not concurrently, on purpose: this VPS has 2
cores, a cycle can have dozens of chunks once large partitions are split,
and each batch is a real `claude -p` process making real API calls --
running them all at once would contend for CPU and make failures harder
to attribute to a specific batch. A batch failure never aborts the cycle;
it's recorded and the driver moves on to the next chunk, matching
run_agent()'s own never-raises contract.
"""
import json

from ingestion.db import get_live_conn
from ingestion.state import now_utc
from wiki_ingestion.detect import build_all_candidates, run_detection
from wiki_ingestion.run_agent import run_agent
from wiki_ingestion.linear_report import (
    start_cycle_parent, finish_cycle_parent, start_batch_task, finish_batch_task,
)

# Rows per sub-agent call. Chosen to keep each call's candidate list (and
# the resulting need to pull full context on each via read tools) small
# enough for focused synthesis in one pass -- not derived from any hard
# limit, just an empirical "keep it tractable" number.
CHUNK_SIZE = 25


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


def _get_cycle_linear_parent(cycle_id):
    conn = get_live_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT linear_parent_issue_id FROM wiki_ingest_cycles WHERE id = %s", (cycle_id,))
            row = cur.fetchone()
            return row["linear_parent_issue_id"] if row else None
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


def _chunk(rows, size):
    return [rows[i:i + size] for i in range(0, len(rows), size)]


def run_cycle(advance_cursors=True, timeout_seconds=1200):
    """Runs one full wiki-ingestion cycle: detect changed rows across every
    partition, split each partition into CHUNK_SIZE-row chunks, then run
    one sub-agent per chunk, sequentially. Returns a summary dict; never
    raises -- a batch failure is recorded and the driver moves on to the
    next chunk, matching run_agent()'s never-raises contract."""
    cycle_id = _start_cycle()
    parent_issue_id = start_cycle_parent(cycle_id)
    _set_cycle_linear_parent(cycle_id, parent_issue_id)
    partitions = run_detection(cycle_id, advance_cursors=advance_cursors)

    batches = []
    for source_kind, rows in partitions.items():
        chunks = _chunk(rows, CHUNK_SIZE)
        for chunk_index, chunk_rows in enumerate(chunks):
            task_issue_id = start_batch_task(parent_issue_id, cycle_id, source_kind, chunk_index + 1, len(chunks), chunk_rows)
            since_ts = now_utc()
            batch_id = _start_batch(cycle_id, source_kind, len(chunk_rows), task_issue_id)
            result = run_agent(cycle_id, source_kind, chunk_rows, timeout_seconds=timeout_seconds)
            finish_batch_task(task_issue_id, cycle_id, source_kind, chunk_index + 1, len(chunks), result, since_ts)
            label = f"{source_kind}[{chunk_index + 1}/{len(chunks)}]" if len(chunks) > 1 else source_kind
            if result["ok"]:
                _finish_batch(batch_id, "done")
                batches.append({"source_kind": label, "row_count": len(chunk_rows), "status": "done"})
            else:
                error = (result.get("stderr") or "")[-2000:] or f"nonzero exit {result.get('returncode')}"
                _finish_batch(batch_id, "ingest_failed", error)
                batches.append({"source_kind": label, "row_count": len(chunk_rows), "status": "ingest_failed", "error": error})

    summary = {
        "partitions_total": len(partitions),
        "batches_total": len(batches),
        "batches_failed": sum(1 for b in batches if b["status"] == "ingest_failed"),
        "batches": batches,
    }
    cycle_status = "failed" if summary["batches_failed"] and summary["batches_failed"] == len(batches) and batches else "done"
    summary["status"] = cycle_status
    _finish_cycle(cycle_id, cycle_status, summary)
    finish_cycle_parent(parent_issue_id, cycle_id, summary)
    result = {"cycle_id": cycle_id, **summary}
    return result


def _reap_orphaned_batches(cycle_id):
    """Marks any batch still 'running' for this cycle as failed -- means the
    driver process died mid-batch (e.g. killed externally) without getting to
    record an outcome. Returns the set of source_kinds that had an orphan, so
    the caller knows to redo that source_kind's next chunk rather than
    trusting its row_count as a completed one."""
    conn = get_live_conn()
    orphaned_kinds = set()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, source_kind FROM wiki_ingest_batches WHERE cycle_id = %s AND status = 'running'",
                (cycle_id,),
            )
            orphans = cur.fetchall()
            for o in orphans:
                orphaned_kinds.add(o["source_kind"])
            cur.execute(
                """UPDATE wiki_ingest_batches SET status = 'ingest_failed',
                   error = 'interrupted: driver process died mid-batch', finished_at = now()
                   WHERE cycle_id = %s AND status = 'running'""",
                (cycle_id,),
            )
        conn.commit()
    finally:
        conn.close()
    return orphaned_kinds


def _done_chunk_counts(cycle_id):
    """Count of 'done' batches per source_kind for this cycle so far --
    since chunks for a given source_kind always run in the same fixed order
    (see _chunk), this count doubles as 'how many leading chunks are done',
    letting resume_cycle skip them and continue from the next one."""
    conn = get_live_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT source_kind, count(*) AS n FROM wiki_ingest_batches
                   WHERE cycle_id = %s AND status = 'done' GROUP BY source_kind""",
                (cycle_id,),
            )
            return {r["source_kind"]: r["n"] for r in cur.fetchall()}
    finally:
        conn.close()


def _existing_batch_summaries(cycle_id):
    conn = get_live_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT source_kind, status, row_count, error FROM wiki_ingest_batches WHERE cycle_id = %s ORDER BY id",
                (cycle_id,),
            )
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def resume_cycle(cycle_id, timeout_seconds=1200):
    """Continues an interrupted cycle (status='running' in wiki_ingest_cycles
    with unfinished batches) without redoing already-done chunks or
    re-triggering detection's dedup/cursor side effects. Rebuilds the exact
    same candidate set the original run_detection call produced (assuming
    since=None full-backfill, matching a from-scratch Phase 3 run -- correct
    for this cycle, since cursors were reset before it started) via
    detect.build_all_candidates(), then replays the same chunk-by-chunk loop
    as run_cycle(), skipping any (source_kind, chunk_index) already 'done'.

    Any batch left 'running' (the process died mid-call) is reaped first and
    its chunk is redone -- run_agent() calls are not guaranteed idempotent
    (a partially-finished sub-agent may have already written some staging
    pages before dying), so a redone chunk can produce duplicate-looking
    drafts; the review sweep (Phase 5) is the intended place to catch that,
    not this resume path."""
    orphaned = _reap_orphaned_batches(cycle_id)
    done_counts = _done_chunk_counts(cycle_id)

    parent_issue_id = _get_cycle_linear_parent(cycle_id)
    if not parent_issue_id:
        parent_issue_id = start_cycle_parent(cycle_id)
        _set_cycle_linear_parent(cycle_id, parent_issue_id)

    partitions = build_all_candidates()

    new_batches = []
    for source_kind, rows in partitions.items():
        if not rows:
            continue
        chunks = _chunk(rows, CHUNK_SIZE)
        start_index = done_counts.get(source_kind, 0)
        if start_index >= len(chunks):
            continue
        for chunk_index in range(start_index, len(chunks)):
            chunk_rows = chunks[chunk_index]
            task_issue_id = start_batch_task(parent_issue_id, cycle_id, source_kind, chunk_index + 1, len(chunks), chunk_rows)
            since_ts = now_utc()
            batch_id = _start_batch(cycle_id, source_kind, len(chunk_rows), task_issue_id)
            result = run_agent(cycle_id, source_kind, chunk_rows, timeout_seconds=timeout_seconds)
            finish_batch_task(task_issue_id, cycle_id, source_kind, chunk_index + 1, len(chunks), result, since_ts)
            label = f"{source_kind}[{chunk_index + 1}/{len(chunks)}]"
            if result["ok"]:
                _finish_batch(batch_id, "done")
                new_batches.append({"source_kind": label, "row_count": len(chunk_rows), "status": "done"})
            else:
                error = (result.get("stderr") or "")[-2000:] or f"nonzero exit {result.get('returncode')}"
                _finish_batch(batch_id, "ingest_failed", error)
                new_batches.append({"source_kind": label, "row_count": len(chunk_rows), "status": "ingest_failed", "error": error})

    all_batches = _existing_batch_summaries(cycle_id)
    summary = {
        "partitions_total": len([k for k, v in partitions.items() if v]),
        "batches_total": len(all_batches),
        "batches_failed": sum(1 for b in all_batches if b["status"] == "ingest_failed"),
        "resumed_orphaned_kinds": sorted(orphaned),
        "batches": all_batches,
    }
    cycle_status = "failed" if summary["batches_failed"] and summary["batches_failed"] == len(all_batches) and all_batches else "done"
    summary["status"] = cycle_status
    _finish_cycle(cycle_id, cycle_status, summary)
    finish_cycle_parent(parent_issue_id, cycle_id, summary)
    result = {"cycle_id": cycle_id, **summary}
    return result


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "resume":
        result = resume_cycle(int(sys.argv[2]))
    else:
        result = run_cycle()
    print(json.dumps(result, indent=2))
