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
from wiki_ingestion.detect import run_detection
from wiki_ingestion.run_agent import run_agent

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


def _start_batch(cycle_id, source_kind, row_count):
    conn = get_live_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO wiki_ingest_batches (cycle_id, source_kind, row_count) VALUES (%s, %s, %s) RETURNING id",
                (cycle_id, source_kind, row_count),
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
    partitions = run_detection(cycle_id, advance_cursors=advance_cursors)

    batches = []
    for source_kind, rows in partitions.items():
        chunks = _chunk(rows, CHUNK_SIZE)
        for chunk_index, chunk_rows in enumerate(chunks):
            batch_id = _start_batch(cycle_id, source_kind, len(chunk_rows))
            result = run_agent(cycle_id, source_kind, chunk_rows, timeout_seconds=timeout_seconds)
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
    _finish_cycle(cycle_id, cycle_status, summary)
    return {"cycle_id": cycle_id, "status": cycle_status, **summary}


if __name__ == "__main__":
    result = run_cycle()
    print(json.dumps(result, indent=2))
