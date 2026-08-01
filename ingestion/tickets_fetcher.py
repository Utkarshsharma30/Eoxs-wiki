"""EOXS support ticket fetcher -- ports the logic from the n8n workflow
"support ticket wiki ingestion v10" (shared directly by the user; no
Python reference existed for this source, unlike every other fetcher in
this package). Tickets are project.task records on a SEPARATE, CENTRAL
Odoo instance (teams.eoxs.com / db Eoxteams_12Feb24), filtered to a
single project (id 76) -- NOT the per-client instances odoo_fetcher.py
connects to for implementation Kanban data, even though it's the same
underlying Odoo model.

Reuses odoo_fetcher.py's generic helpers verbatim (OdooClient, m2o_name,
strip_html, fetch_messages via message_format(), classify_message,
fetch_attachments, _is_retryable) rather than duplicating them -- these
were already written model-agnostically.

Incremental via project.task.write_date (matching the n8n workflow's own
primary mechanism), tracked through the standard sync_cursors table
(source="eoxs_tickets") instead of n8n's GitHub-stored JSON state file.
Unlike n8n's byte-for-byte GitHub attachment commits, attachment content
is metadata only here (filename/mimetype/size) -- same v1 scope decision
as every other Odoo-derived attachment source in this system.

Usage: python -m ingestion.tickets_fetcher [--dry-run] [--limit N]
"""
import argparse
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ingestion.db import dual_write, get_live_conn
from ingestion.state import sync_since, set_last_synced_at, now_utc
from ingestion.write_ticket import write_ticket
from ingestion.odoo_fetcher import (
    OdooClient, TASK_FIELDS, m2o_name, strip_html,
    fetch_messages, classify_message, fetch_attachments, _is_retryable,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("ingestion.tickets")

SOURCE = "eoxs_tickets"
BASE_URL = "https://teams.eoxs.com"
DB = "Eoxteams_12Feb24"
SUPPORT_PROJECT_ID = 76
DEFAULT_MAX_RESULTS = 2000
DEFAULT_SAFETY_OVERLAP_DAYS = 2


def ticket_number(task_id):
    return f"T{task_id:05d}"


def fetch_tag_names(client, tag_ids):
    if not tag_ids:
        return {}
    rows = client.search_read("project.tags", [["id", "in", tag_ids]], ["id", "name"])
    return {r["id"]: r["name"] for r in rows}


def resolve_client_id(client_raw):
    """Best-effort match against clients.display_name -- tickets.client_raw
    is a freetext partner display name, not an email/domain, so
    ingestion.routing's email-based matching doesn't apply here."""
    if not client_raw:
        return None
    conn = get_live_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM clients WHERE display_name ILIKE %s LIMIT 1", (client_raw,))
            row = cur.fetchone()
            return row["id"] if row else None
    finally:
        conn.close()


def build_ticket_record(task, tag_map, messages_by_task, attachments_by_task):
    msgs = sorted(messages_by_task.get(task["id"], []), key=lambda m: str(m.get("date") or ""))
    events = []
    for msg in msgs:
        body_text = strip_html(msg.get("body"))
        if not body_text and not msg.get("tracking"):
            continue
        events.append({
            "odoo_msg_id": msg["id"],
            "event_type": classify_message(msg),
            "author": m2o_name(msg.get("author_id")),
            "event_time": msg.get("date") or None,
            "body": body_text,
        })

    attachments = [
        {
            "filename": att.get("name") or f"attachment-{att['id']}",
            "mimetype": att.get("mimetype"),
            "size_bytes": att.get("file_size"),
        }
        for att in attachments_by_task.get(task["id"], [])
    ]

    priority = "High" if str(task.get("priority")) in ("1", "true", "True") else "Normal"
    tag_ids = task.get("tag_ids") or []
    client_raw = m2o_name(task.get("partner_id"))
    if client_raw == "—":
        client_raw = None

    return {
        "odoo_id": task["id"],
        "ticket_number": ticket_number(task["id"]),
        "client_raw": client_raw,
        "client_id": resolve_client_id(client_raw),
        "subject": task.get("name") or "(no subject)",
        "status": m2o_name(task.get("stage_id")),
        "priority": priority,
        "assigned_to": m2o_name(task.get("user_id")),
        "ticket_created": (str(task.get("create_date") or "")[:10] or None),
        "ticket_closed": (str(task.get("date_end") or "")[:10] or None) if task.get("date_end") else None,
        "tags": [tag_map.get(tid, str(tid)) for tid in tag_ids],
        "description": strip_html(task.get("description")),
        "generated_at": now_utc(),
        "events": events,
        "attachments": attachments,
    }


def process_tickets(*, dry_run=False, limit=DEFAULT_MAX_RESULTS,
                     safety_overlap_days=DEFAULT_SAFETY_OVERLAP_DAYS):
    username = os.environ["EOXS_TICKETS_ODOO_USERNAME"]
    password = os.environ["EOXS_TICKETS_ODOO_PASSWORD"]

    since = sync_since(SOURCE, safety_overlap_days)
    logger.info("source=%s since=%s limit=%d dry_run=%s", SOURCE, since, limit, dry_run)

    client = OdooClient(BASE_URL, DB, username, password)

    domain = [["project_id", "=", SUPPORT_PROJECT_ID]]
    if since:
        domain.append(["write_date", ">=", since.strftime("%Y-%m-%d %H:%M:%S")])

    tasks = client.search_read("project.task", domain, TASK_FIELDS, order="id asc", limit=limit)
    logger.info("source=%s candidate tickets=%d", SOURCE, len(tasks))

    counts = {"written": 0, "error": 0}
    run_started_at = now_utc()

    if not tasks:
        if not dry_run:
            set_last_synced_at(SOURCE, run_started_at)
        logger.info("source=%s done: %s", SOURCE, counts)
        return counts

    task_ids = [t["id"] for t in tasks]
    msg_ids = client.search_ids("mail.message", [["model", "=", "project.task"], ["res_id", "in", task_ids]])
    messages = fetch_messages(client, msg_ids)
    attachments = fetch_attachments(client, task_ids, msg_ids)
    logger.info("source=%s %d messages, %d attachments", SOURCE, len(messages), len(attachments))

    all_tag_ids = sorted({tid for t in tasks for tid in (t.get("tag_ids") or [])})
    tag_map = fetch_tag_names(client, all_tag_ids)

    messages_by_task = {}
    for m in messages:
        messages_by_task.setdefault(m["res_id"], []).append(m)

    msg_to_task = {m["id"]: m["res_id"] for m in messages}
    attachments_by_task = {}
    for att in attachments:
        if att["res_model"] == "project.task":
            attachments_by_task.setdefault(att["res_id"], []).append(att)
        elif att["res_model"] == "mail.message":
            tid = msg_to_task.get(att["res_id"])
            if tid:
                attachments_by_task.setdefault(tid, []).append(att)

    for task in tasks:
        try:
            record = build_ticket_record(task, tag_map, messages_by_task, attachments_by_task)

            if dry_run:
                logger.info("[dry-run] would write ticket %s: %r", record["ticket_number"], record["subject"])
                counts["written"] += 1
                continue

            dual_write(write_ticket, **record)
            counts["written"] += 1

        except Exception as e:
            logger.error("ticket odoo_id=%s failed: %s", task.get("id"), e)
            counts["error"] += 1

    if not dry_run:
        set_last_synced_at(SOURCE, run_started_at)

    logger.info("source=%s done: %s", SOURCE, counts)
    return counts


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=DEFAULT_MAX_RESULTS)
    args = parser.parse_args()

    process_tickets(dry_run=args.dry_run, limit=args.limit)


if __name__ == "__main__":
    main()
