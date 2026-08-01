"""Loads raw/tickets/*.md into tickets / ticket_events / ticket_attachments.
Idempotent: upserts on ticket_number; skips unchanged files via db_sync_state.
"""
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loaders._db import get_conn, VAULT_ROOT
from parsers.frontmatter import parse_frontmatter
from parsers.ticket_body import extract_description, extract_events

SOURCE_TYPE = "ticket"


def to_date(value):
    if value is None or value == "":
        return None
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def to_dt(value):
    if not value:
        return None
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def to_str_list(value):
    if not value:
        return []
    return [str(v) for v in value]


def load_file(conn, path):
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
    if not meta.get("ticket_number"):
        return "no_ticket_number"

    description = extract_description(body)
    events = extract_events(body)

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO tickets (
                odoo_id, ticket_number, client_raw, subject, status, priority,
                assigned_to, ticket_created, ticket_closed, tags, description,
                generated_at, source_file_path, source_file_mtime
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (ticket_number) DO UPDATE SET
                odoo_id = EXCLUDED.odoo_id,
                client_raw = EXCLUDED.client_raw,
                subject = EXCLUDED.subject,
                status = EXCLUDED.status,
                priority = EXCLUDED.priority,
                assigned_to = EXCLUDED.assigned_to,
                ticket_created = EXCLUDED.ticket_created,
                ticket_closed = EXCLUDED.ticket_closed,
                tags = EXCLUDED.tags,
                description = EXCLUDED.description,
                generated_at = EXCLUDED.generated_at,
                source_file_path = EXCLUDED.source_file_path,
                source_file_mtime = EXCLUDED.source_file_mtime,
                updated_at = now()
            RETURNING id
            """,
            (
                meta.get("odoo_id"), meta["ticket_number"], meta.get("client"),
                meta.get("subject"), meta.get("status"), meta.get("priority"),
                meta.get("assigned_to"), to_date(meta.get("created")), to_date(meta.get("closed")),
                to_str_list(meta.get("tags")), description, to_dt(meta.get("generated_at")),
                rel_path, mtime,
            ),
        )
        ticket_id = cur.fetchone()[0]

        cur.execute("DELETE FROM ticket_events WHERE ticket_id = %s", (ticket_id,))
        cur.execute("DELETE FROM ticket_attachments WHERE ticket_id = %s", (ticket_id,))

        for ev in events:
            event_time = None
            if ev["event_time_str"]:
                try:
                    event_time = datetime.strptime(ev["event_time_str"], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
                except ValueError:
                    pass
            cur.execute(
                """
                INSERT INTO ticket_events (ticket_id, odoo_msg_id, event_type, author, event_time, body, event_order)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                """,
                (ticket_id, ev["odoo_msg_id"], ev["event_type"], ev["author"], event_time, ev["body"], ev["event_order"]),
            )

        attachments_dir = VAULT_ROOT / "raw/tickets/attachments" / meta["ticket_number"]
        if attachments_dir.is_dir():
            for att_path in attachments_dir.iterdir():
                if att_path.is_file():
                    cur.execute(
                        "INSERT INTO ticket_attachments (ticket_id, filename, relative_path) VALUES (%s,%s,%s)",
                        (ticket_id, att_path.name, str(att_path.relative_to(VAULT_ROOT))),
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
    root = VAULT_ROOT / "raw/tickets"
    counts = {"loaded": 0, "skipped": 0, "no_ticket_number": 0, "error": 0}
    n = 0
    for path in root.glob("*.md"):
        try:
            result = load_file(conn, path)
            counts[result] += 1
        except Exception as e:
            conn.rollback()
            print(f"FAIL {path.name}: {e}")
            counts["error"] += 1
        n += 1
    conn.close()
    print(f"scanned {n} files")
    print("totals:", counts)


if __name__ == "__main__":
    main()
