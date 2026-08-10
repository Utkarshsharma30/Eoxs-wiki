"""Writes a fetched sale order (+ line items, activity, linked invoices)
into sales_orders / order_lines / sales_order_events / invoices /
invoice_lines. Upsert key: order_number -- matches the existing UNIQUE
constraint, same natural key the historical file-based loader used, so
this and loaders/load_invoices.py coexist without duplicate rows (a
historical order gets "graduated" into the live-maintained table the
first time Odoo reports it changed, same as write_ticket.py/write_email.py
do for their own historical predecessors).

access_tier deliberately excluded from DO UPDATE SET (same convention as
every other tiered table) so a re-synced order's tier, once computed,
survives future refreshes. order_lines/sales_order_events/invoices/
invoice_lines are NOT independently tiered -- they're only ever reachable
through get_invoice(), which checks the parent sales_order's access_tier
before returning anything, same pattern as ticket_events/
implementation_task_events.

Called via ingestion.db.dual_write() so it runs once against live and
once against staging.
"""


def write_sales_order(conn, *, odoo_id, order_number, client_raw, client_id, order_date, expiry_date,
                       amount_total, currency, state, state_label, salesperson, client_order_ref,
                       linked_invoice_numbers, generated_at, order_lines, events, invoices, access_tier="tier2"):
    """order_lines: list of dicts {product, description, qty, delivered, invoiced, unit_price,
    discount_pct, subtotal} -- the sale order's OWN lines (sale.order.line), distinct from each
    linked invoice's own line items below.
    events: list of dicts {odoo_msg_id, message_type, author, event_time, body}.
    invoices: list of dicts {odoo_id, invoice_number, state, invoice_date, due_date, amount_untaxed,
    amount_tax, amount_total, amount_paid, amount_due, payment_state, invoice_sent, narration, lines}.
    Returns the sales_order's DB id (from the live connection's perspective when
    called via dual_write; staging's return value is discarded by the caller)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO sales_orders (
                odoo_id, order_number, client_raw, client_id, order_date, expiry_date,
                amount_total, currency, state, state_label, salesperson, client_order_ref,
                linked_invoices, generated_at, access_tier, source_file_path, source_file_mtime
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NULL,now())
            ON CONFLICT (order_number) DO UPDATE SET
                odoo_id = EXCLUDED.odoo_id,
                client_raw = EXCLUDED.client_raw,
                client_id = EXCLUDED.client_id,
                order_date = EXCLUDED.order_date,
                expiry_date = EXCLUDED.expiry_date,
                amount_total = EXCLUDED.amount_total,
                currency = EXCLUDED.currency,
                state = EXCLUDED.state,
                state_label = EXCLUDED.state_label,
                salesperson = EXCLUDED.salesperson,
                client_order_ref = EXCLUDED.client_order_ref,
                linked_invoices = EXCLUDED.linked_invoices,
                generated_at = EXCLUDED.generated_at,
                updated_at = now()
            RETURNING id
            """,
            (
                odoo_id, order_number, client_raw, client_id, order_date, expiry_date,
                amount_total, currency, state, state_label, salesperson, client_order_ref,
                linked_invoice_numbers, generated_at, access_tier,
            ),
        )
        order_id = cur.fetchone()["id"]

        cur.execute("DELETE FROM order_lines WHERE sales_order_id = %s", (order_id,))
        for line_pos, line in enumerate(order_lines or [], start=1):
            cur.execute(
                """
                INSERT INTO order_lines (
                    sales_order_id, product, description, qty, delivered, invoiced,
                    unit_price, discount_pct, subtotal, line_order
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (order_id, line.get("product"), line.get("description"), line.get("qty"),
                 line.get("delivered"), line.get("invoiced"), line.get("unit_price"),
                 line.get("discount_pct"), line.get("subtotal"), line_pos),
            )

        cur.execute("DELETE FROM sales_order_events WHERE sales_order_id = %s", (order_id,))
        for order_pos, ev in enumerate(events or [], start=1):
            cur.execute(
                """
                INSERT INTO sales_order_events (
                    sales_order_id, odoo_msg_id, message_type, author, event_time, body, event_order
                ) VALUES (%s,%s,%s,%s,%s,%s,%s)
                """,
                (order_id, ev.get("odoo_msg_id"), ev.get("message_type"), ev.get("author"),
                 ev.get("event_time"), ev["body"], order_pos),
            )

        cur.execute("DELETE FROM invoices WHERE sales_order_id = %s", (order_id,))
        for inv in invoices or []:
            cur.execute(
                """
                INSERT INTO invoices (
                    sales_order_id, odoo_id, invoice_number, state, invoice_date, due_date,
                    amount_untaxed, amount_tax, amount_total, amount_paid, amount_due,
                    payment_state, invoice_sent, narration
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING id
                """,
                (
                    order_id, inv["odoo_id"], inv.get("invoice_number"), inv.get("state"),
                    inv.get("invoice_date"), inv.get("due_date"), inv.get("amount_untaxed"),
                    inv.get("amount_tax"), inv.get("amount_total"), inv.get("amount_paid"),
                    inv.get("amount_due"), inv.get("payment_state"), inv.get("invoice_sent", False),
                    inv.get("narration"),
                ),
            )
            invoice_id = cur.fetchone()["id"]
            for line_pos, line in enumerate(inv.get("lines") or [], start=1):
                cur.execute(
                    """
                    INSERT INTO invoice_lines (
                        invoice_id, product, description, qty, unit_price, discount_pct, subtotal, line_order
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (invoice_id, line.get("product"), line.get("description"), line.get("qty"),
                     line.get("unit_price"), line.get("discount_pct"), line.get("subtotal"), line_pos),
                )

    conn.commit()
    return order_id
