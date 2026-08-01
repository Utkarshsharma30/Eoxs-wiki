"""Sync cursor + cross-thread dedup state, backed by sync_cursors and
message_ids_seen in the LIVE database (staging never drives cursor
decisions -- see design: cursor advances only after the live write
succeeds, staging is best-effort and never blocks or gates anything).
"""
from datetime import datetime, timedelta, timezone

from ingestion.db import get_live_conn


def get_last_synced_at(source):
    conn = get_live_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT last_synced_at FROM sync_cursors WHERE source = %s", (source,))
            row = cur.fetchone()
            return row["last_synced_at"] if row else None
    finally:
        conn.close()


def set_last_synced_at(source, when):
    conn = get_live_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO sync_cursors (source, last_synced_at)
                VALUES (%s, %s)
                ON CONFLICT (source) DO UPDATE SET
                    last_synced_at = EXCLUDED.last_synced_at,
                    updated_at = now()
                """,
                (source, when),
            )
        conn.commit()
    finally:
        conn.close()


def sync_since(source, safety_overlap_days=2):
    """Returns the timestamp to fetch-from: last cursor minus a safety
    overlap window (tolerates clock skew / late-arriving webhooks, same
    rationale as the Render pipeline's gmail_after_date()). None if this
    source has never synced (caller should apply its own first-run default)."""
    last = get_last_synced_at(source)
    if last is None:
        return None
    return last - timedelta(days=safety_overlap_days)


def is_message_seen(message_id):
    conn = get_live_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM message_ids_seen WHERE message_id = %s", (message_id,))
            return cur.fetchone() is not None
    finally:
        conn.close()


def mark_messages_seen(message_ids, source_account, thread_id=None):
    if not message_ids:
        return
    conn = get_live_conn()
    try:
        with conn.cursor() as cur:
            for mid in message_ids:
                cur.execute(
                    """
                    INSERT INTO message_ids_seen (message_id, thread_source_account, thread_id)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (message_id) DO NOTHING
                    """,
                    (mid, source_account, thread_id),
                )
        conn.commit()
    finally:
        conn.close()


def now_utc():
    return datetime.now(timezone.utc)
