"""Writes a client's fully-refreshed Odoo implementation tasks into
implementation_tasks / implementation_task_events / implementation_task_attachments.

Unlike every other write layer in this package, this is a FULL REFRESH,
not an upsert: deletes all of a client's existing task rows (cascades to
events/attachments via ON DELETE CASCADE) and reinserts fresh, mirroring
the old pipeline's "wipe the output folder, rewrite everything" behavior
for this Odoo-derived, never-hand-edited data (see schema/014's comment).
Called via ingestion.db.dual_write() so it runs once against live and
once against staging.
"""
from psycopg2.extras import Json


def write_client_tasks(conn, *, client_id, tasks):
    """tasks: list of dicts with task fields + 'events': [...] + 'attachments': [...].
    Returns the number of tasks written (from the live connection's
    perspective when called via dual_write)."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM implementation_tasks WHERE client_id = %s", (client_id,))

        for task in tasks:
            cur.execute(
                """
                INSERT INTO implementation_tasks (
                    client_id, odoo_task_id, project_name, task_name, stage, owner,
                    priority, kanban_state, active, description, task_created_date,
                    task_updated_date, deadline, generated_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING id
                """,
                (
                    client_id, task["odoo_task_id"], task.get("project_name"), task["task_name"],
                    task.get("stage"), task.get("owner"), task.get("priority"),
                    task.get("kanban_state"), task.get("active", True), task.get("description"),
                    task.get("task_created_date"), task.get("task_updated_date"), task.get("deadline"),
                    task.get("generated_at"),
                ),
            )
            task_id = cur.fetchone()["id"]

            for order, event in enumerate(task.get("events") or [], start=1):
                cur.execute(
                    """
                    INSERT INTO implementation_task_events (
                        task_id, odoo_msg_id, event_type, author, event_time,
                        body, tracking_changes, event_order
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        task_id, event.get("odoo_msg_id"), event["event_type"], event.get("author"),
                        event.get("event_time"), event["body"], Json(event.get("tracking_changes") or []),
                        order,
                    ),
                )

            for att in task.get("attachments") or []:
                cur.execute(
                    """
                    INSERT INTO implementation_task_attachments (
                        task_id, odoo_attachment_id, filename, mimetype, size_bytes
                    ) VALUES (%s,%s,%s,%s,%s)
                    """,
                    (task_id, att["odoo_attachment_id"], att["filename"], att.get("mimetype"), att.get("size_bytes")),
                )

    conn.commit()
    return len(tasks)
