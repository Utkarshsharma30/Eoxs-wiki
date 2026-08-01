"""Writes a fetched EOXS support ticket into tickets / ticket_events /
ticket_attachments. Upsert key: ticket_number -- matches the existing
UNIQUE constraint, same natural key the file-based loader uses (both
derive it as 'T' + zero-padded Odoo task id), so this and
loaders/load_tickets.py can coexist without duplicate rows. Follows
write_email.py's exact pattern: takes conn first, full DELETE+reinsert of
children, commits internally, returns the parent row id. Called via
ingestion.db.dual_write() so it runs once against live and once against
staging.
"""


def write_ticket(conn, *, odoo_id, ticket_number, client_raw, client_id, subject, status,
                  priority, assigned_to, ticket_created, ticket_closed, tags, description,
                  generated_at, events, attachments):
    """events: list of dicts {odoo_msg_id, event_type, author, event_time, body}.
    attachments: list of dicts {filename, mimetype, size_bytes}.
    Returns the ticket's DB id (from the live connection's perspective when
    called via dual_write; staging's return value is discarded by the caller)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO tickets (
                odoo_id, ticket_number, client_raw, client_id, subject, status, priority,
                assigned_to, ticket_created, ticket_closed, tags, description,
                generated_at, source_file_path, source_file_mtime
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NULL,now())
            ON CONFLICT (ticket_number) DO UPDATE SET
                odoo_id = EXCLUDED.odoo_id,
                client_raw = EXCLUDED.client_raw,
                client_id = EXCLUDED.client_id,
                subject = EXCLUDED.subject,
                status = EXCLUDED.status,
                priority = EXCLUDED.priority,
                assigned_to = EXCLUDED.assigned_to,
                ticket_created = EXCLUDED.ticket_created,
                ticket_closed = EXCLUDED.ticket_closed,
                tags = EXCLUDED.tags,
                description = EXCLUDED.description,
                generated_at = EXCLUDED.generated_at,
                updated_at = now()
            RETURNING id
            """,
            (
                odoo_id, ticket_number, client_raw, client_id, subject, status, priority,
                assigned_to, ticket_created, ticket_closed, tags, description, generated_at,
            ),
        )
        ticket_id = cur.fetchone()["id"]

        cur.execute("DELETE FROM ticket_events WHERE ticket_id = %s", (ticket_id,))
        cur.execute("DELETE FROM ticket_attachments WHERE ticket_id = %s", (ticket_id,))

        for order, event in enumerate(events, start=1):
            cur.execute(
                """
                INSERT INTO ticket_events (ticket_id, odoo_msg_id, event_type, author, event_time, body, event_order)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    ticket_id, event.get("odoo_msg_id"), event["event_type"], event.get("author"),
                    event.get("event_time"), event["body"], order,
                ),
            )

        for att in attachments:
            cur.execute(
                "INSERT INTO ticket_attachments (ticket_id, filename, mimetype, size_bytes) VALUES (%s,%s,%s,%s)",
                (ticket_id, att["filename"], att.get("mimetype"), att.get("size_bytes")),
            )

    conn.commit()
    return ticket_id
