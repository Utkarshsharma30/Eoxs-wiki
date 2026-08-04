"""Writes a client's Odoo implementation tasks into implementation_tasks /
implementation_task_events / implementation_task_attachments.

Upserts on (client_id, odoo_task_id) -- the stable natural key that
already existed as a UNIQUE constraint (schema/014) but went unused until
now. Previously this was a full DELETE+INSERT every run (see git history
for schema/014's original comment on why: purely-Odoo-derived data,
no human-edit-protection needed, matching the old pipeline). That
stopped being a safe simplification once access_tier (Cruz's tiered
access system) needed to persist on a task across refreshes -- a full
wipe silently reset every task's tier to the column default every 2
hours, found live. The explicit column list below deliberately excludes
access_tier so upserts never touch it, mirroring write_email.py's/
write_ticket.py's existing pattern for the same reason.

Events/attachments (this task's children) are still fully replaced on
every write, same as before and same as every other write layer's
children -- Odoo's chatter can grow/change and there's no cheap
incremental signal for it, so full-replace-per-parent stays correct.

Tasks no longer returned by Odoo (real deletions, not just archival --
Odoo already reports archival via its own `active` field on the task
itself) are reconciled by the caller via mark_tasks_inactive() after all
of a client's tasks have been upserted, so a genuinely-removed task
doesn't stay a permanent zombie row now that nothing wipes the table
first.

Called via ingestion.db.dual_write() so it runs once against live and
once against staging.
"""
from psycopg2.extras import Json


def write_client_tasks(conn, *, client_id, tasks):
    """tasks: list of dicts with task fields + 'events': [...] + 'attachments': [...].
    Returns the number of tasks written (from the live connection's
    perspective when called via dual_write)."""
    with conn.cursor() as cur:
        for task in tasks:
            cur.execute(
                """
                INSERT INTO implementation_tasks (
                    client_id, odoo_task_id, project_name, task_name, stage, owner,
                    priority, kanban_state, active, description, task_created_date,
                    task_updated_date, deadline, generated_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (client_id, odoo_task_id) DO UPDATE SET
                    project_name = EXCLUDED.project_name,
                    task_name = EXCLUDED.task_name,
                    stage = EXCLUDED.stage,
                    owner = EXCLUDED.owner,
                    priority = EXCLUDED.priority,
                    kanban_state = EXCLUDED.kanban_state,
                    active = EXCLUDED.active,
                    description = EXCLUDED.description,
                    task_created_date = EXCLUDED.task_created_date,
                    task_updated_date = EXCLUDED.task_updated_date,
                    deadline = EXCLUDED.deadline,
                    generated_at = EXCLUDED.generated_at,
                    updated_at = now()
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

            cur.execute("DELETE FROM implementation_task_events WHERE task_id = %s", (task_id,))
            cur.execute("DELETE FROM implementation_task_attachments WHERE task_id = %s", (task_id,))

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


def mark_tasks_inactive(conn, *, client_id, seen_odoo_task_ids):
    """Reconciliation pass, called once per client after all of its tasks
    for this run have been upserted: any existing task NOT in
    seen_odoo_task_ids wasn't returned by Odoo this time at all (a real
    deletion, distinct from Odoo's own `active=false` archival, which
    already comes through as a normal field update above) -- mark it
    inactive rather than leave it stale forever, now that nothing wipes
    the table first. Never deletes the row -- access_tier and history
    stay intact either way, matching this table's upsert-not-wipe
    philosophy everywhere else."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE implementation_tasks SET active = false, updated_at = now() "
            "WHERE client_id = %s AND active = true AND NOT (odoo_task_id = ANY(%s))",
            (client_id, list(seen_odoo_task_ids)),
        )
        count = cur.rowcount
    conn.commit()
    return count
