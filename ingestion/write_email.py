"""Writes a fetched email thread (Gmail or Zoho) into email_threads /
email_messages / email_attachments. Upsert key: (source_account,
gmail_thread_id) -- same natural key the file-based loader uses, so this
and loaders/load_emails.py can coexist without creating duplicate rows for
the same thread. Called via ingestion.db.dual_write() so it runs once
against live and once against staging.
"""
from datetime import timezone


def write_thread(conn, *, source_account, gmail_thread_id, subject, from_addr, to_addr,
                  message_count, participants, thread_dates, tags, is_quarantined,
                  generated_at, messages, attachments_by_message_index=None, client_id=None):
    """messages: list of dicts {message_index, message_date, from_addr, body, message_ids}
    attachments_by_message_index: dict {message_index: [{filename, relative_path, size_bytes, note}]}
    client_id: from ingestion.routing.classify_client(index, participants), or None if no
    participant matched a known client contact/domain -- caller's responsibility to classify,
    this just stores the result (matches call_transcripts' existing client_id pattern).
    Returns the thread's DB id (from the live connection's perspective when
    called via dual_write; staging's return value is discarded by the caller)."""
    attachments_by_message_index = attachments_by_message_index or {}

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO email_threads (
                source_account, gmail_thread_id, subject, from_addr, to_addr,
                message_count, participants, thread_dates, tags, is_quarantined,
                generated_at, client_id, source_file_path, source_file_mtime
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NULL,now())
            ON CONFLICT (source_account, gmail_thread_id) DO UPDATE SET
                subject = EXCLUDED.subject,
                from_addr = EXCLUDED.from_addr,
                to_addr = EXCLUDED.to_addr,
                message_count = EXCLUDED.message_count,
                participants = EXCLUDED.participants,
                thread_dates = EXCLUDED.thread_dates,
                tags = EXCLUDED.tags,
                is_quarantined = EXCLUDED.is_quarantined,
                generated_at = EXCLUDED.generated_at,
                client_id = EXCLUDED.client_id,
                updated_at = now()
            RETURNING id
            """,
            (
                source_account, gmail_thread_id, subject, from_addr, to_addr,
                message_count, participants, thread_dates, tags, is_quarantined,
                generated_at, client_id,
            ),
        )
        thread_id = cur.fetchone()["id"]

        cur.execute("DELETE FROM email_messages WHERE thread_id = %s", (thread_id,))
        cur.execute("DELETE FROM email_attachments WHERE thread_id = %s", (thread_id,))

        for msg in messages:
            msg_date = msg["message_date"]
            if msg_date and msg_date.tzinfo is None:
                msg_date = msg_date.replace(tzinfo=timezone.utc)

            cur.execute(
                """
                INSERT INTO email_messages (thread_id, message_index, message_date, from_addr, body)
                VALUES (%s,%s,%s,%s,%s)
                RETURNING id
                """,
                (thread_id, msg["message_index"], msg_date, msg["from_addr"], msg["body"]),
            )
            message_id = cur.fetchone()["id"]

            for att in attachments_by_message_index.get(msg["message_index"], []):
                cur.execute(
                    """
                    INSERT INTO email_attachments (thread_id, message_id, filename, relative_path, size_bytes, note)
                    VALUES (%s,%s,%s,%s,%s,%s)
                    """,
                    (thread_id, message_id, att["filename"], att.get("relative_path"),
                     att.get("size_bytes"), att.get("note")),
                )

    conn.commit()
    return thread_id


def existing_message_count(conn, source_account, gmail_thread_id):
    """Mirrors the file-based dedup check: if a thread already exists with a
    message_count >= the freshly-fetched one, there's nothing new to write."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT message_count FROM email_threads WHERE source_account = %s AND gmail_thread_id = %s",
            (source_account, gmail_thread_id),
        )
        row = cur.fetchone()
        return row["message_count"] if row else None
