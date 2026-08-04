"""Odoo implementation-task fetcher -- ports the XML-RPC fetch logic from
the Render pipeline's tools/odoo_implementation_to_obsidian.py: per-client
project.task Kanban data + mail.message chatter (via message_format(),
which surfaces tracking-value diffs even for restricted accounts) +
ir.attachment metadata (metadata only -- see schema/014's comment on why
attachment bytes aren't fetched here). client config (base_url/db/
project_names/env var names) is hardcoded below rather than read from the
vault repo's tools/config.yaml at runtime, matching zoho_fetcher.py's
ZOHO_ACCOUNT_ID precedent -- small, static, rarely-changing reference
data has no reason to depend on the vault repo.

FULL REFRESH every run, not incremental -- this data is entirely
Odoo-derived and never hand-edited, so there's no human-edit-protection
concern and no cursor/dedup complexity needed (matches the old pipeline's
"delete the output folder, rewrite everything" behavior exactly).
sync_cursors still gets a row per client (source="odoo_<slug>") after a
successful run, for observability parity with the other sources, but
nothing reads it back to gate or filter a fetch.

Usage: python -m ingestion.odoo_fetcher --client greer [--dry-run]
       python -m ingestion.odoo_fetcher --all [--dry-run]
"""
import argparse
import base64
import logging
import os
import re
import sys
import xmlrpc.client
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ingestion.db import dual_write, get_live_conn
from ingestion.state import set_last_synced_at, now_utc
from ingestion.write_implementation import write_client_tasks, mark_tasks_inactive
from ingestion.retry import call_with_retry

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("ingestion.odoo")

# Mirrors tools/config.yaml's odoo_implementation.clients block in the old
# pipeline. client_slug matches clients.slug (seeded via loaders/load_clients.py)
# so implementation_tasks.client_id can be resolved by a simple lookup.
ODOO_CLIENTS = [
    {"id": "greer", "client_slug": "greer-steel", "base_url": "https://greersteel.eoxs.com", "db": "greer",
     "project_names": ["CRM Implementation"], "username_env": "GREER_ODOO_USERNAME", "password_env": "GREER_ODOO_PASSWORD"},
    {"id": "ess", "client_slug": "eastern-states-steel", "base_url": "https://ess.eoxs.com", "db": "ess",
     "project_names": ["Implementation - Phase 1"], "username_env": "ESS_ODOO_USERNAME", "password_env": "ESS_ODOO_PASSWORD"},
    {"id": "dps", "client_slug": "discount-pipe-steel", "base_url": "https://discountpipesteel.eoxs.com", "db": "discount_pipe2",
     "project_names": ["Implementation"], "username_env": "DPS_ODOO_USERNAME", "password_env": "DPS_ODOO_PASSWORD"},
    {"id": "ppc", "client_slug": "ppc-metals", "base_url": "https://ppc.eoxs.com", "db": "velox",
     "project_names": ["Implementation Tasks"], "username_env": "PPC_ODOO_USERNAME", "password_env": "PPC_ODOO_PASSWORD"},
    {"id": "3gm", "client_slug": "3gm-steel", "base_url": "https://3gm.eoxs.com", "db": "threegmsteels",
     "project_names": ["1. Kick off/Discovery Calls", "5. Master Data Import"],
     "username_env": "THREEGM_ODOO_USERNAME", "password_env": "THREEGM_ODOO_PASSWORD"},
    {"id": "sabre", "client_slug": "sabre-alloys", "base_url": "https://sabre.eoxs.com", "db": "sabre",
     "project_names": ["Soft Launch"], "username_env": "SABRE_ODOO_USERNAME", "password_env": "SABRE_ODOO_PASSWORD"},
]

TASK_FIELDS = [
    "id", "name", "description", "stage_id", "user_id", "partner_id", "project_id",
    "priority", "kanban_state", "create_date", "write_date", "date_deadline",
    "date_end", "tag_ids", "active", "parent_id", "child_ids",
]
ATTACHMENT_FIELDS = ["id", "name", "res_model", "res_id", "mimetype", "file_size", "create_date"]


def _is_retryable(e):
    if isinstance(e, xmlrpc.client.Fault):
        return False  # application-level RPC error (bad args, auth) -- retrying won't help
    return isinstance(e, (ConnectionError, TimeoutError, OSError, xmlrpc.client.ProtocolError))


def m2o_name(val):
    """Odoo many2one fields come back as [id, 'Display Name'] or False."""
    if isinstance(val, (list, tuple)) and len(val) == 2:
        return str(val[1])
    return "—"


def strip_html(html):
    if not html:
        return ""
    text = html
    text = re.sub(r"<a[^>]*>([^<]*)</a>", r"\1", text)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</p>", "\n", text, flags=re.I)
    text = re.sub(r"</div>", "\n", text, flags=re.I)
    text = re.sub(
        r"<li>([\s\S]*?)</li>",
        lambda m: "- " + re.sub(r"<[^>]+>", "", m.group(1)).strip() + "\n",
        text, flags=re.I,
    )
    text = re.sub(r"<tr[^>]*>", "\n", text, flags=re.I)
    text = re.sub(r"</t[dh]>", " | ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r" \|[ \t]*\n", "\n", text)
    text = (
        text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
        .replace("&quot;", '"').replace("&#39;", "'").replace("&nbsp;", " ")
    )
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


class OdooClient:
    def __init__(self, base_url, db, username, password):
        self.base_url = base_url.rstrip("/")
        self.db = db
        self.username = username
        self.password = password
        common = xmlrpc.client.ServerProxy(f"{self.base_url}/xmlrpc/2/common")
        self.uid = call_with_retry(lambda: common.authenticate(db, username, password, {}), is_retryable=_is_retryable)
        if not self.uid:
            raise RuntimeError(f"Odoo auth failed for {base_url} (db={db}, user={username})")
        self.models = xmlrpc.client.ServerProxy(f"{self.base_url}/xmlrpc/2/object")

    def execute_kw(self, model, method, args, kwargs=None):
        return call_with_retry(
            lambda: self.models.execute_kw(self.db, self.uid, self.password, model, method, args, kwargs or {}),
            is_retryable=_is_retryable,
        )

    def search_read(self, model, domain, fields, limit=0, offset=0, order=""):
        results = []
        batch = 500
        off = offset
        while True:
            opts = {"fields": fields, "limit": batch, "offset": off}
            if order:
                opts["order"] = order
            page = self.execute_kw(model, "search_read", [domain], opts)
            results.extend(page)
            if len(page) == 0 or (limit and len(results) >= limit):
                break
            off += batch
        return results[:limit] if limit else results

    def search_ids(self, model, domain):
        results = []
        batch = 2000
        off = 0
        while True:
            page = self.execute_kw(model, "search", [domain], {"limit": batch, "offset": off})
            results.extend(page)
            if len(page) == 0:
                break
            off += batch
        return results


def fetch_messages(client, msg_ids):
    """mail.message.message_format() -- same code path Odoo's web client
    uses to render chatter, surfaces tracking-value diffs even for
    accounts whose group membership blocks a direct mail.tracking.value read."""
    messages = []
    chunk_size = 300
    for i in range(0, len(msg_ids), chunk_size):
        chunk = msg_ids[i:i + chunk_size]
        for row in client.execute_kw("mail.message", "message_format", [chunk]):
            tracking = []
            for tv in row.get("tracking_value_ids") or []:
                field = tv.get("changed_field") or ""
                if not field:
                    continue
                old_val = tv.get("old_value")
                new_val = tv.get("new_value")
                old_val = "" if old_val in (False, None) else str(old_val)
                new_val = "" if new_val in (False, None) else str(new_val)
                if not (old_val.strip() or new_val.strip()):
                    continue
                tracking.append({"field": field, "old": old_val, "new": new_val})
            messages.append({
                "id": row["id"], "res_id": row.get("res_id"), "author_id": row.get("author_id"),
                "date": row.get("date"), "body": row.get("body"),
                "message_type": row.get("message_type"), "subtype_id": row.get("subtype_id"),
                "tracking": tracking,
            })
    return messages


def classify_message(msg):
    subtype = m2o_name(msg.get("subtype_id")).lower()
    msg_type = msg.get("message_type") or ""
    tracking = msg.get("tracking") or []
    if any("stage" in t["field"].lower() for t in tracking):
        return "stage_change"
    if msg_type == "comment" and ("log" in subtype or "note" in subtype):
        return "log_note"
    if msg_type == "email":
        return "email"
    return "other"


def fetch_attachments(client, task_ids, msg_ids):
    branches = []
    if task_ids:
        branches.append(["&", ["res_model", "=", "project.task"], ["res_id", "in", task_ids]])
    if msg_ids:
        branches.append(["&", ["res_model", "=", "mail.message"], ["res_id", "in", msg_ids]])
    if not branches:
        return []
    domain = ["|", *branches[0], *branches[1]] if len(branches) == 2 else branches[0]
    return client.search_read("ir.attachment", domain, ATTACHMENT_FIELDS)


def find_project(client, project_name):
    """Includes archived projects -- several clients keep their
    implementation-phase project archived once go-live is done."""
    active_either = ["|", ["active", "=", True], ["active", "=", False]]
    projects = client.search_read("project.project", ["&", ["name", "=", project_name], *active_either], ["id", "name"])
    if not projects:
        projects = client.search_read("project.project", ["&", ["name", "ilike", project_name], *active_either], ["id", "name"])
    return projects[0] if projects else None


def _resolve_client_id(client_slug):
    conn = get_live_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM clients WHERE slug = %s", (client_slug,))
            row = cur.fetchone()
            return row["id"] if row else None
    finally:
        conn.close()


def build_task_record(task, messages_by_task, attachments_by_task):
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
            "tracking_changes": msg.get("tracking") or [],
        })

    attachments = [
        {
            "odoo_attachment_id": att["id"],
            "filename": att.get("name") or f"attachment-{att['id']}",
            "mimetype": att.get("mimetype"),
            "size_bytes": att.get("file_size"),
        }
        for att in attachments_by_task.get(task["id"], [])
    ]

    return {
        "odoo_task_id": task["id"],
        "project_name": m2o_name(task.get("project_id")),
        "task_name": str(task.get("name") or "Untitled Task"),
        "stage": m2o_name(task.get("stage_id")),
        "owner": m2o_name(task.get("user_id")),
        "priority": "High" if str(task.get("priority")) in ("1", "true", "True") else "Normal",
        "kanban_state": task.get("kanban_state") or "normal",
        "active": bool(task.get("active", True)),
        "description": strip_html(task.get("description")),
        "task_created_date": (str(task.get("create_date") or "")[:10] or None),
        "task_updated_date": (str(task.get("write_date") or "")[:10] or None),
        "deadline": (str(task.get("date_deadline") or "")[:10] or None),
        "generated_at": now_utc(),
        "events": events,
        "attachments": attachments,
    }


def process_client(cfg, *, dry_run=False):
    client_id = _resolve_client_id(cfg["client_slug"])
    if client_id is None:
        raise RuntimeError(f"clients.slug={cfg['client_slug']!r} not found -- run loaders/load_clients.py first")

    username = os.environ.get(cfg["username_env"], "")
    password = os.environ.get(cfg["password_env"], "")
    if not username or not password:
        raise RuntimeError(f"missing {cfg['username_env']}/{cfg['password_env']} in .env")

    logger.info("client=%s connecting to %s (db=%s)", cfg["id"], cfg["base_url"], cfg["db"])
    client = OdooClient(cfg["base_url"], cfg["db"], username, password)

    project_ids = []
    for name in cfg["project_names"]:
        proj = find_project(client, name)
        if not proj:
            logger.error("client=%s no project matching %r (checked archived too)", cfg["id"], name)
            continue
        project_ids.append(proj["id"])

    if not project_ids:
        raise RuntimeError(f"client={cfg['id']}: none of the configured projects were found")

    domain = ["&", ["project_id", "in", project_ids], "|", ["active", "=", True], ["active", "=", False]]
    tasks = client.search_read("project.task", domain, TASK_FIELDS, order="id asc")
    logger.info("client=%s %d tasks found", cfg["id"], len(tasks))

    if not tasks:
        if not dry_run:
            dual_write(write_client_tasks, client_id=client_id, tasks=[])
            # write_client_tasks(tasks=[]) is now a no-op upsert (nothing to upsert) --
            # unlike the old full-wipe behavior, it does NOT clear existing tasks on its
            # own, so this client reporting zero tasks needs the reconciliation pass too.
            dual_write(mark_tasks_inactive, client_id=client_id, seen_odoo_task_ids=[])
            set_last_synced_at(f"odoo_{cfg['id']}", now_utc())
        return 0

    task_ids = [t["id"] for t in tasks]
    msg_ids = client.search_ids("mail.message", [["model", "=", "project.task"], ["res_id", "in", task_ids]])
    messages = fetch_messages(client, msg_ids)
    attachments = fetch_attachments(client, task_ids, msg_ids)
    logger.info("client=%s %d messages, %d attachments", cfg["id"], len(messages), len(attachments))

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

    task_records = [build_task_record(t, messages_by_task, attachments_by_task) for t in tasks]

    if dry_run:
        logger.info("[dry-run] client=%s would write %d tasks", cfg["id"], len(task_records))
        return len(task_records)

    written = dual_write(write_client_tasks, client_id=client_id, tasks=task_records)
    seen_ids = [t["odoo_task_id"] for t in task_records]
    deactivated = dual_write(mark_tasks_inactive, client_id=client_id, seen_odoo_task_ids=seen_ids)
    set_last_synced_at(f"odoo_{cfg['id']}", now_utc())
    logger.info("client=%s wrote %d tasks, deactivated %d no-longer-present tasks", cfg["id"], written, deactivated)
    return written


def process_all(*, dry_run=False, only_client=None):
    clients = [c for c in ODOO_CLIENTS if c["id"] == only_client] if only_client else ODOO_CLIENTS
    results = {}
    for cfg in clients:
        try:
            results[cfg["id"]] = process_client(cfg, dry_run=dry_run)
        except Exception as e:
            logger.error("client=%s failed: %s", cfg["id"], e)
            results[cfg["id"]] = None
    logger.info("done: %s", results)
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--client", choices=[c["id"] for c in ODOO_CLIENTS])
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not args.client and not args.all:
        parser.error("specify --client <id> or --all")

    process_all(dry_run=args.dry_run, only_client=args.client)


if __name__ == "__main__":
    main()
