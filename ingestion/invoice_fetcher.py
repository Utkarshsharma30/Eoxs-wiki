"""EOXS sales-order/invoice fetcher -- ports the logic from the n8n
workflow "invoice wiki ingestion automation v4 (github)" (shared directly
by the user). sale.order lives on the SAME central Odoo instance as
tickets (teams.eoxs.com / db Eoxteams_12Feb24, same credentials) -- NOT
the per-client instances odoo_fetcher.py connects to for implementation
Kanban data, even though both fetchers exist in this file's sibling
modules for different models.

Reuses odoo_fetcher.py's generic helpers verbatim (OdooClient, m2o_name,
strip_html, fetch_messages via message_format(), fetch_attachments,
_is_retryable) rather than duplicating them.

Four Odoo models feed one sales_order record:
  sale.order        -- the order itself
  sale.order.line    -- its own line items (order_lines)
  mail.message       -- order-level chatter/activity (sales_order_events)
  account.move (+ account.move.line) -- linked invoices actually generated
    against this order, matched via invoice_origin, each with amounts,
    payment status, and their own line items (invoices / invoice_lines)

Incremental via sale.order.write_date (matching the n8n workflow's own
mechanism), tracked through the standard sync_cursors table
(source="eoxs_invoices") instead of n8n's GitHub-stored JSON state file.

Historical note: 185 sales_orders already exist from the old file-based
loader (raw/invoices/*.md, source_file_path set). This fetcher upserts on
order_number (the same natural key that loader used), so a historical
order gets "graduated" into live-maintained status the first time Odoo
reports it changed -- same pattern as write_ticket.py/write_email.py
relative to their own historical predecessors.

Usage: python -m ingestion.invoice_fetcher [--dry-run] [--limit N]
"""
import argparse
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ingestion.db import dual_write, get_live_conn
from ingestion.state import sync_since, set_last_synced_at, now_utc
from ingestion.write_invoice import write_sales_order
from ingestion.inline_tier_classifier import classify_tier
from ingestion.odoo_fetcher import OdooClient, m2o_name, strip_html, fetch_messages, _is_retryable

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("ingestion.invoices")

SOURCE = "eoxs_invoices"
BASE_URL = "https://teams.eoxs.com"
DB = "Eoxteams_12Feb24"
DEFAULT_MAX_RESULTS = 2000
DEFAULT_SAFETY_OVERLAP_DAYS = 2

ORDER_FIELDS = [
    "name", "date_order", "validity_date", "amount_total", "currency_id", "state",
    "partner_id", "user_id", "order_line", "write_date", "client_order_ref", "note",
]
ORDER_LINE_FIELDS = [
    "id", "order_id", "name", "product_id", "product_uom_qty", "qty_delivered",
    "qty_invoiced", "price_unit", "discount", "price_subtotal",
]
MOVE_FIELDS = [
    "id", "name", "state", "invoice_date", "invoice_date_due", "amount_untaxed", "amount_tax",
    "amount_total", "amount_residual", "invoice_payment_state", "invoice_origin",
    "invoice_sent", "narration", "invoice_line_ids",
]
MOVE_LINE_FIELDS = [
    "id", "name", "quantity", "price_unit", "discount", "price_subtotal",
    "price_total", "exclude_from_invoice_tab", "display_type", "product_id",
]

STATE_LABELS = {
    "draft": "Quotation", "sent": "Quotation Sent", "sale": "Sales Order",
    "done": "Locked", "cancel": "Cancelled",
}


def resolve_client_id(client_raw):
    """Best-effort match against clients.display_name -- same approach as
    tickets_fetcher.py's resolve_client_id (sales_orders.client_raw is a
    freetext partner display name too, not an email/domain)."""
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


def fetch_order_lines(client, order_ids):
    if not order_ids:
        return {}
    lines = client.search_read("sale.order.line", [["order_id", "in", order_ids]], ORDER_LINE_FIELDS)
    by_order = {}
    for line in lines:
        oid = line["order_id"][0] if isinstance(line.get("order_id"), (list, tuple)) else line.get("order_id")
        by_order.setdefault(oid, []).append(line)
    return by_order


def fetch_order_messages(client, order_ids):
    """mail.message on sale.order -- comment (human notes) + non-empty
    notification (system events), matching the n8n workflow's filter."""
    if not order_ids:
        return {}
    msg_ids = client.search_ids("mail.message", [
        ["model", "=", "sale.order"], ["res_id", "in", order_ids],
        ["message_type", "in", ["comment", "notification"]],
    ])
    messages = fetch_messages(client, msg_ids)
    by_order = {}
    for m in messages:
        body_text = strip_html(m.get("body"))
        if m.get("message_type") == "notification" and not body_text:
            continue
        by_order.setdefault(m["res_id"], []).append({
            "odoo_msg_id": m["id"],
            "message_type": m.get("message_type"),
            "author": m2o_name(m.get("author_id")),
            "event_time": m.get("date") or None,
            "body": body_text,
        })
    return by_order


def fetch_linked_invoices(client, orders):
    """account.move (type=out_invoice, not cancelled) matched via
    invoice_origin against this batch's order names -- one invoice's
    invoice_origin can cover multiple orders (comma-separated), matching
    the n8n workflow's split-and-match logic."""
    order_names = [o["name"] for o in orders if o.get("name")]
    if not order_names:
        return {}
    moves = client.search_read("account.move", [
        ["invoice_origin", "in", order_names],
        ["type", "=", "out_invoice"],
        ["state", "!=", "cancel"],
    ], MOVE_FIELDS)

    # Real line ids live in each move's own invoice_line_ids field -- NOT the
    # move ids themselves. Collect those first, across every move, before
    # calling account.move.line at all.
    line_ids_by_move = {}
    all_line_ids = []
    for mv in moves:
        ids = mv.get("invoice_line_ids") or []
        ids = ids if isinstance(ids, list) else []
        line_ids_by_move[mv["id"]] = ids
        all_line_ids.extend(ids)

    lines_by_id = {}
    if all_line_ids:
        move_lines = client.search_read("account.move.line", [["id", "in", all_line_ids]], MOVE_LINE_FIELDS)
        for line in move_lines:
            if line.get("exclude_from_invoice_tab") or line.get("display_type"):
                continue
            lines_by_id[line["id"]] = line

    name_to_order = {o["name"]: o for o in orders if o.get("name")}
    by_order = {}
    for mv in moves:
        origin = mv.get("invoice_origin") or ""
        matched_orders = [name_to_order[orig] for orig in (s.strip() for s in origin.split(","))
                           if orig.strip() in name_to_order]
        if not matched_orders:
            continue

        total = mv.get("amount_total") or 0
        residual = mv.get("amount_residual")
        residual = residual if isinstance(residual, (int, float)) else total
        paid = max(0, total - residual)
        lines = [
            {
                "product": m2o_name(l.get("product_id")),
                "description": l.get("name"),
                "qty": l.get("quantity"),
                "unit_price": l.get("price_unit"),
                "discount_pct": l.get("discount"),
                "subtotal": l.get("price_subtotal"),
            }
            for lid in line_ids_by_move.get(mv["id"], []) if lid in lines_by_id
            for l in [lines_by_id[lid]]
        ]
        invoice_record = {
            "odoo_id": mv["id"],
            "invoice_number": mv.get("name"),
            "state": mv.get("state"),
            "invoice_date": mv.get("invoice_date") or None,
            "due_date": mv.get("invoice_date_due") or None,
            "amount_untaxed": mv.get("amount_untaxed"),
            "amount_tax": mv.get("amount_tax"),
            "amount_total": total,
            "amount_paid": paid,
            "amount_due": residual,
            "payment_state": mv.get("invoice_payment_state"),
            "invoice_sent": bool(mv.get("invoice_sent")),
            "narration": strip_html(mv.get("narration")) or None,
            "lines": lines,
        }
        for order in matched_orders:
            # Same move object referenced once per matched order -- an invoice
            # spanning multiple orders appears fully under each (matches the
            # n8n workflow's own per-order markdown generation).
            by_order.setdefault(order["id"], []).append(dict(invoice_record))

    return by_order


def build_sales_order_record(order, lines_by_order, messages_by_order, invoices_by_order):
    client_raw = m2o_name(order.get("partner_id"))
    if client_raw == "—":
        client_raw = None
    currency = m2o_name(order.get("currency_id"))
    if currency == "—":
        currency = "USD"
    order_date = (str(order.get("date_order") or "")[:10] or None)
    expiry_date = (str(order.get("validity_date") or "")[:10] or None) or order_date
    state = order.get("state") or ""
    client_order_ref = order.get("client_order_ref")
    if client_order_ref in (False, None, ""):
        client_order_ref = None

    order_lines = [
        {
            "product": m2o_name(line.get("product_id")),
            "description": line.get("name"),
            "qty": line.get("product_uom_qty"),
            "delivered": line.get("qty_delivered"),
            "invoiced": line.get("qty_invoiced"),
            "unit_price": line.get("price_unit"),
            "discount_pct": line.get("discount"),
            "subtotal": line.get("price_subtotal"),
        }
        for line in lines_by_order.get(order["id"], [])
    ]
    invoices = invoices_by_order.get(order["id"], [])
    linked_invoice_numbers = [inv["invoice_number"] for inv in invoices if inv.get("invoice_number")]

    return {
        "odoo_id": order["id"],
        "order_number": order.get("name") or f"ODOO-{order['id']}",
        "client_raw": client_raw,
        "client_id": resolve_client_id(client_raw),
        "order_date": order_date,
        "expiry_date": expiry_date,
        "amount_total": order.get("amount_total") or 0,
        "currency": currency,
        "state": state,
        "state_label": STATE_LABELS.get(state, state),
        "salesperson": m2o_name(order.get("user_id")),
        "client_order_ref": client_order_ref,
        "linked_invoice_numbers": linked_invoice_numbers,
        "generated_at": now_utc(),
        "order_lines": order_lines,
        "events": messages_by_order.get(order["id"], []),
        "invoices": invoices,
    }


def process_invoices(*, dry_run=False, limit=DEFAULT_MAX_RESULTS,
                      safety_overlap_days=DEFAULT_SAFETY_OVERLAP_DAYS):
    username = os.environ["EOXS_TICKETS_ODOO_USERNAME"]
    password = os.environ["EOXS_TICKETS_ODOO_PASSWORD"]

    since = sync_since(SOURCE, safety_overlap_days)
    logger.info("source=%s since=%s limit=%d dry_run=%s", SOURCE, since, limit, dry_run)

    client = OdooClient(BASE_URL, DB, username, password)

    domain = []
    if since:
        domain.append(["write_date", ">=", since.strftime("%Y-%m-%d %H:%M:%S")])

    orders = client.search_read("sale.order", domain, ORDER_FIELDS, order="date_order desc", limit=limit)
    logger.info("source=%s candidate orders=%d", SOURCE, len(orders))

    counts = {"written": 0, "error": 0, "written_items": []}
    run_started_at = now_utc()

    if not orders:
        if not dry_run:
            set_last_synced_at(SOURCE, run_started_at)
        logger.info("source=%s done: %s", SOURCE, counts)
        return counts

    order_ids = [o["id"] for o in orders]
    lines_by_order = fetch_order_lines(client, order_ids)
    messages_by_order = fetch_order_messages(client, order_ids)
    invoices_by_order = fetch_linked_invoices(client, orders)
    logger.info("source=%s %d orders with lines, %d with messages, %d with invoices", SOURCE,
                len(lines_by_order), len(messages_by_order), len(invoices_by_order))

    for order in orders:
        try:
            record = build_sales_order_record(order, lines_by_order, messages_by_order, invoices_by_order)

            if dry_run:
                logger.info("[dry-run] would write order %s: client=%s amount=%s",
                            record["order_number"], record["client_raw"], record["amount_total"])
                counts["written"] += 1
                counts["written_items"].append(record["order_number"])
                continue

            product_names = ", ".join(l["product"] for l in record["order_lines"][:5] if l.get("product"))
            tier_context = (
                f"Sales order {record['order_number']} for client {record['client_raw']}\n"
                f"Products: {product_names}\n"
                f"Customer reference: {record['client_order_ref'] or ''}"
            )
            record["access_tier"] = classify_tier(tier_context)
            dual_write(write_sales_order, **record)
            counts["written"] += 1
            counts["written_items"].append(record["order_number"])

        except Exception as e:
            logger.error("order odoo_id=%s failed: %s", order.get("id"), e)
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

    process_invoices(dry_run=args.dry_run, limit=args.limit)


if __name__ == "__main__":
    main()
