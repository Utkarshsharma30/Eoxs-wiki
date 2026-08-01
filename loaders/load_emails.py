"""Loads raw/emails/{raj_gmail,ron_gmail,remya_gmail,support_zoho}/**/*.md
and raw/_spam_quarantine/{raj_gmail,ron_gmail}/**/*.md into
email_threads / email_messages / email_attachments.

Idempotent: upserts on (source_account, gmail_thread_id); skips files whose
mtime hasn't changed since the last load (tracked in db_sync_state).
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loaders._db import get_conn, VAULT_ROOT
from parsers.frontmatter import parse_frontmatter
from parsers.email_body import split_messages
from parsers.email_attachments import extract_attachments

SOURCE_TYPE = "email_thread"

# (folder relative to VAULT_ROOT, source_account, is_quarantined)
EMAIL_ROOTS = [
    ("raw/emails/raj_gmail", "raj_gmail", False),
    ("raw/emails/ron_gmail", "ron_gmail", False),
    ("raw/emails/remya_gmail", "remya_gmail", False),
    ("raw/emails/support_zoho", "support_zoho", False),
    ("raw/_spam_quarantine/raj_gmail", "raj_gmail", True),
    ("raw/_spam_quarantine/ron_gmail", "ron_gmail", True),
]


def iter_thread_files(root_rel):
    root = VAULT_ROOT / root_rel
    if not root.exists():
        return
    for path in root.rglob("*.md"):
        if "attachments" in path.relative_to(root).parts:
            continue
        yield path


def to_dt(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def load_file(conn, path, source_account, is_quarantined):
    rel_path = str(path.relative_to(VAULT_ROOT))
    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT file_mtime FROM db_sync_state WHERE source_type = %s AND source_file_path = %s",
            (SOURCE_TYPE, rel_path),
        )
        row = cur.fetchone()
        if row and row[0] >= mtime:
            return "skipped"

    text = path.read_text(encoding="utf-8")
    meta, body = parse_frontmatter(text)
    if not meta.get("gmail_thread_id"):
        return "no_thread_id"

    thread_dates = [to_dt(d) for d in meta.get("dates", [])]

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO email_threads (
                source_account, gmail_thread_id, subject, from_addr, to_addr,
                message_count, participants, thread_dates, tags, is_quarantined,
                generated_at, generated_hash, source_file_path, source_file_mtime
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
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
                generated_hash = EXCLUDED.generated_hash,
                source_file_path = EXCLUDED.source_file_path,
                source_file_mtime = EXCLUDED.source_file_mtime,
                updated_at = now()
            RETURNING id
            """,
            (
                source_account, meta["gmail_thread_id"], meta.get("subject"),
                meta.get("from"), meta.get("to"), meta.get("message_count"),
                meta.get("participants", []), thread_dates, meta.get("tags", []),
                is_quarantined, to_dt(meta.get("generated_at")), meta.get("generated_hash"),
                rel_path, mtime,
            ),
        )
        thread_id = cur.fetchone()[0]

        cur.execute("DELETE FROM email_messages WHERE thread_id = %s", (thread_id,))
        cur.execute("DELETE FROM email_attachments WHERE thread_id = %s", (thread_id,))

        messages = split_messages(body)
        for msg in messages:
            msg_date = None
            try:
                msg_date = datetime.strptime(msg["message_date_str"], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
            except ValueError:
                pass

            cur.execute(
                """
                INSERT INTO email_messages (thread_id, message_index, message_date, from_addr, body)
                VALUES (%s,%s,%s,%s,%s)
                RETURNING id
                """,
                (thread_id, msg["message_index"], msg_date, msg["from_addr"], msg["body"]),
            )
            message_id = cur.fetchone()[0]

            attachments_dir = f"raw/emails/{source_account}/attachments/{meta['gmail_thread_id']}"
            for att in extract_attachments(msg["body"]):
                cur.execute(
                    """
                    INSERT INTO email_attachments (thread_id, message_id, filename, relative_path, size_bytes, note)
                    VALUES (%s,%s,%s,%s,%s,%s)
                    """,
                    (thread_id, message_id, att["filename"],
                     f"{attachments_dir}/{att['filename']}", att["size_bytes"], att["note"]),
                )

        cur.execute(
            """
            INSERT INTO db_sync_state (source_type, source_file_path, file_mtime)
            VALUES (%s,%s,%s)
            ON CONFLICT (source_type, source_file_path) DO UPDATE SET
                file_mtime = EXCLUDED.file_mtime, last_loaded_at = now()
            """,
            (SOURCE_TYPE, rel_path, mtime),
        )
    conn.commit()
    return "loaded"


def main():
    conn = get_conn()
    counts = {"loaded": 0, "skipped": 0, "no_thread_id": 0}
    for root_rel, source_account, is_quarantined in EMAIL_ROOTS:
        n = 0
        for path in iter_thread_files(root_rel):
            try:
                result = load_file(conn, path, source_account, is_quarantined)
                counts[result] += 1
            except Exception as e:
                conn.rollback()
                print(f"FAIL {path}: {e}")
                counts.setdefault("error", 0)
                counts["error"] += 1
            n += 1
        print(f"{root_rel}: scanned {n} files")
    conn.close()
    print("totals:", counts)


if __name__ == "__main__":
    main()
