"""Detection stage for the wiki-ingestion pipeline: partitions new/changed
raw rows by source category, mirroring the old wiki-agent's raw/ folder
categories (raj_gmail, ron_gmail, remya_gmail, support_zoho, tickets,
calls, clients/<slug>) so each category can be handed to its own
sub-agent later (Phase 2/3).

Cursors reuse the existing sync_cursors table with a "wiki_ingest_"
prefix, distinct from raw-ingestion's own cursor keys on the same table.

Content-hash dedup (wiki_ingest_seen) exists specifically because
implementation_tasks does a full delete+reinsert every raw-ingestion
sweep -- every row's updated_at changes even when nothing about the row
actually did, which would otherwise make every implementation task look
"changed" on every wiki-ingestion cycle and waste real LLM cost
reprocessing identical content. The same hash-then-compare approach is
applied uniformly across all source kinds, not just that one, since it's
cheap and correct regardless of source.
"""
import hashlib

from ingestion.db import get_live_conn
from ingestion.state import now_utc

EMAIL_ACCOUNTS = ["raj_gmail", "ron_gmail", "remya_gmail", "support_zoho"]


def _hash_content(*parts):
    h = hashlib.sha256()
    for p in parts:
        h.update((str(p) if p is not None else "").encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def get_cursor(source_kind):
    conn = get_live_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT last_synced_at FROM sync_cursors WHERE source = %s", (f"wiki_ingest_{source_kind}",))
            row = cur.fetchone()
            return row["last_synced_at"] if row else None
    finally:
        conn.close()


def set_cursor(source_kind, when):
    conn = get_live_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO sync_cursors (source, last_synced_at) VALUES (%s, %s)
                ON CONFLICT (source) DO UPDATE SET last_synced_at = EXCLUDED.last_synced_at, updated_at = now()
                """,
                (f"wiki_ingest_{source_kind}", when),
            )
        conn.commit()
    finally:
        conn.close()


def _candidate_rows(sql, params):
    conn = get_live_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()
    finally:
        conn.close()


def _email_content(thread_id):
    conn = get_live_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT body FROM email_messages WHERE thread_id = %s ORDER BY message_index", (thread_id,))
            return "\n".join(r["body"] or "" for r in cur.fetchall())
    finally:
        conn.close()


def candidates_email(source_account, since):
    sql = "SELECT id, subject, participants, message_count, updated_at FROM email_threads WHERE source_account = %s"
    params = [source_account]
    if since:
        sql += " AND updated_at > %s"
        params.append(since)
    sql += " ORDER BY updated_at ASC"
    rows = _candidate_rows(sql, params)
    for row in rows:
        row["_content_hash"] = _hash_content(row["subject"], row["participants"], row["message_count"], _email_content(row["id"]))
    return rows


def _ticket_content(ticket_id):
    conn = get_live_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT body FROM ticket_events WHERE ticket_id = %s ORDER BY event_order", (ticket_id,))
            return "\n".join(r["body"] or "" for r in cur.fetchall())
    finally:
        conn.close()


def candidates_tickets(since):
    sql = "SELECT id, ticket_number, subject, status, priority, description, updated_at FROM tickets WHERE 1=1"
    params = []
    if since:
        sql += " AND updated_at > %s"
        params.append(since)
    sql += " ORDER BY updated_at ASC"
    rows = _candidate_rows(sql, params)
    for row in rows:
        row["_content_hash"] = _hash_content(
            row["subject"], row["status"], row["priority"], row["description"], _ticket_content(row["id"])
        )
    return rows


def _call_content(call_id):
    conn = get_live_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT text FROM call_segments WHERE call_id = %s ORDER BY segment_order", (call_id,))
            return "\n".join(r["text"] or "" for r in cur.fetchall())
    finally:
        conn.close()


def candidates_calls(since):
    sql = "SELECT id, source, meeting_title, call_date, updated_at FROM call_transcripts WHERE 1=1"
    params = []
    if since:
        sql += " AND updated_at > %s"
        params.append(since)
    sql += " ORDER BY updated_at ASC"
    rows = _candidate_rows(sql, params)
    for row in rows:
        row["_content_hash"] = _hash_content(row["meeting_title"], row["call_date"], _call_content(row["id"]))
    return rows


def _implementation_task_content(task_id):
    conn = get_live_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT body FROM implementation_task_events WHERE task_id = %s ORDER BY event_order", (task_id,)
            )
            return "\n".join(r["body"] or "" for r in cur.fetchall())
    finally:
        conn.close()


def candidates_implementation_tasks(client_slug, since):
    """odoo_fetcher.py full-refreshes (DELETE+INSERT) implementation_tasks
    every raw-ingestion sweep, so the serial `id` is NOT a stable
    identifier across runs -- the same real-world task gets a fresh id
    every time. `odoo_task_id` (stable within a client partition) is used
    as the dedup key instead of `id`, unlike every other source kind here."""
    sql = """
        SELECT it.id, it.odoo_task_id, it.task_name, it.stage, it.owner, it.priority,
               it.description, it.updated_at
        FROM implementation_tasks it JOIN clients c ON c.id = it.client_id
        WHERE c.slug = %s
    """
    params = [client_slug]
    if since:
        sql += " AND it.updated_at > %s"
        params.append(since)
    sql += " ORDER BY it.updated_at ASC"
    rows = _candidate_rows(sql, params)
    for row in rows:
        row["_stable_id"] = row["odoo_task_id"]
        row["_content_hash"] = _hash_content(
            row["task_name"], row["stage"], row["owner"], row["priority"], row["description"],
            _implementation_task_content(row["id"]),
        )
    return rows


def filter_unchanged(source_kind, rows, cycle_id=None):
    """Compares each row's precomputed _content_hash against wiki_ingest_seen,
    dropping rows whose content hasn't actually changed. Returns the
    surviving rows and records every decision (processed or
    skipped_unchanged) so it's auditable, never a silent drop.

    Uses row["_stable_id"] as the dedup key when a candidates_* function
    set one (implementation_tasks, whose serial `id` is not stable across
    full-refresh runs -- see candidates_implementation_tasks), falling
    back to row["id"] for every other source kind, where upsert-based
    writes keep `id` stable across re-ingestion."""
    conn = get_live_conn()
    changed = []
    skipped = 0
    try:
        with conn.cursor() as cur:
            for row in rows:
                row_id = row.get("_stable_id", row["id"])
                content_hash = row["_content_hash"]
                cur.execute(
                    "SELECT content_hash FROM wiki_ingest_seen WHERE source_kind = %s AND source_row_id = %s",
                    (source_kind, row_id),
                )
                existing = cur.fetchone()
                if existing and existing["content_hash"] == content_hash:
                    decision = "skipped_unchanged"
                    skipped += 1
                else:
                    decision = "processed"
                    changed.append(row)
                cur.execute(
                    """
                    INSERT INTO wiki_ingest_seen (source_kind, source_row_id, content_hash, decision, last_seen_at, cycle_id)
                    VALUES (%s,%s,%s,%s,now(),%s)
                    ON CONFLICT (source_kind, source_row_id) DO UPDATE SET
                        content_hash = EXCLUDED.content_hash, decision = EXCLUDED.decision,
                        last_seen_at = now(), cycle_id = EXCLUDED.cycle_id
                    """,
                    (source_kind, row_id, content_hash, decision, cycle_id),
                )
        conn.commit()
    finally:
        conn.close()
    return changed, skipped


def list_client_slugs():
    conn = get_live_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT slug FROM clients ORDER BY slug")
            return [r["slug"] for r in cur.fetchall()]
    finally:
        conn.close()


def build_all_candidates(since_by_kind=None):
    """Returns {source_kind: rows} for every partition, WITHOUT filter_unchanged
    or cursor side effects -- the same per-partition queries run_detection uses,
    exposed separately so a resume path can reconstruct an interrupted cycle's
    exact original candidate set (calling run_detection again would find nothing,
    since its own filter_unchanged call already marked everything 'processed').

    since_by_kind: optional {source_kind: since_timestamp} override, defaulting
    to None (full backfill) per partition -- pass this when reconstructing a
    cycle that ran with real cursors rather than a from-scratch backfill."""
    since_by_kind = since_by_kind or {}
    partitions = {}

    for account in EMAIL_ACCOUNTS:
        partitions[account] = candidates_email(account, since_by_kind.get(account))

    partitions["tickets"] = candidates_tickets(since_by_kind.get("tickets"))
    partitions["calls"] = candidates_calls(since_by_kind.get("calls"))

    for slug in list_client_slugs():
        source_kind = f"client_{slug}"
        partitions[source_kind] = candidates_implementation_tasks(slug, since_by_kind.get(source_kind))

    return partitions


def run_detection(cycle_id=None, advance_cursors=True):
    """Runs detection across every partition. Returns {source_kind: [rows]}
    for partitions with at least one changed row -- empty partitions are
    omitted, matching the old pipeline's "no empty cycles on the board."
    """
    run_started_at = now_utc()
    partitions = {}

    for account in EMAIL_ACCOUNTS:
        since = get_cursor(account)
        candidates = candidates_email(account, since)
        changed, skipped = filter_unchanged(account, candidates, cycle_id)
        if changed:
            partitions[account] = changed
        if advance_cursors:
            set_cursor(account, run_started_at)

    since = get_cursor("tickets")
    candidates = candidates_tickets(since)
    changed, skipped = filter_unchanged("tickets", candidates, cycle_id)
    if changed:
        partitions["tickets"] = changed
    if advance_cursors:
        set_cursor("tickets", run_started_at)

    since = get_cursor("calls")
    candidates = candidates_calls(since)
    changed, skipped = filter_unchanged("calls", candidates, cycle_id)
    if changed:
        partitions["calls"] = changed
    if advance_cursors:
        set_cursor("calls", run_started_at)

    for slug in list_client_slugs():
        source_kind = f"client_{slug}"
        since = get_cursor(source_kind)
        candidates = candidates_implementation_tasks(slug, since)
        changed, skipped = filter_unchanged(source_kind, candidates, cycle_id)
        if changed:
            partitions[source_kind] = changed
        if advance_cursors:
            set_cursor(source_kind, run_started_at)

    return partitions
