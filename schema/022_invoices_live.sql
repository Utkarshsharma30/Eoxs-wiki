-- Live invoice/sales-order ingestion -- ports the n8n workflow "invoice
-- wiki ingestion automation v4 (github)" (shared directly by the user),
-- same pattern as schema/014's implementation_tasks and tickets_fetcher.py.
-- sale.order on the SAME central Odoo instance as tickets (teams.eoxs.com /
-- Eoxteams_12Feb24), not the per-client instances.
--
-- sales_orders/order_lines already existed (historical file-load only,
-- schema/009) -- source_file_path was NOT NULL there, which blocks a
-- brand-new live-only order (no historical file) from ever being
-- inserted. Every other live-ingested table already went through this
-- same nullable-path fix (migrations 012/015/016); sales_orders never did
-- because it never had a live fetcher until now.
ALTER TABLE sales_orders ALTER COLUMN source_file_path DROP NOT NULL;

-- client_order_ref: fetched by the n8n workflow ("Customer Reference" in
-- the generated markdown) but not previously a column here.
ALTER TABLE sales_orders ADD COLUMN client_order_ref TEXT;

-- Order-level activity/chatter (mail.message on sale.order) -- same shape
-- as ticket_events/implementation_task_events. Children of a sales_order,
-- protected by the parent's access_tier (see get_invoice in mcp_server) --
-- not independently tiered, matching every other *_events table.
CREATE TABLE sales_order_events (
    id              SERIAL PRIMARY KEY,
    sales_order_id  INTEGER NOT NULL REFERENCES sales_orders(id) ON DELETE CASCADE,
    odoo_msg_id     INTEGER,
    message_type    TEXT,             -- 'comment' | 'notification'
    author          TEXT,
    event_time      TIMESTAMPTZ,
    body            TEXT NOT NULL,
    event_order     INTEGER NOT NULL
);

CREATE INDEX idx_sales_order_events_order ON sales_order_events(sales_order_id);

-- Linked invoices (account.move, type=out_invoice) -- a sale order can
-- have zero, one, or several as it gets invoiced over time. Structured
-- (not squashed into sales_orders.linked_invoices TEXT[], which only ever
-- held bare invoice numbers) so amounts/payment-status/line-items are
-- actually queryable, matching the richness the n8n workflow already
-- captures in its generated markdown. Protected by the parent
-- sales_order's access_tier, same as sales_order_events -- an invoice is
-- never fetched except through get_invoice(), which already checks the
-- parent's tier before returning anything.
CREATE TABLE invoices (
    id                      SERIAL PRIMARY KEY,
    sales_order_id          INTEGER NOT NULL REFERENCES sales_orders(id) ON DELETE CASCADE,
    odoo_id                 INTEGER NOT NULL,
    invoice_number          TEXT,
    state                   TEXT,             -- 'draft' | 'posted' | ...
    invoice_date            DATE,
    due_date                DATE,
    amount_untaxed          NUMERIC(14,2),
    amount_tax              NUMERIC(14,2),
    amount_total            NUMERIC(14,2),
    amount_paid             NUMERIC(14,2),
    amount_due              NUMERIC(14,2),
    payment_state           TEXT,
    invoice_sent            BOOLEAN NOT NULL DEFAULT false,
    narration               TEXT,             -- free-text payment notes
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (sales_order_id, odoo_id)
);

CREATE INDEX idx_invoices_sales_order ON invoices(sales_order_id);

CREATE TABLE invoice_lines (
    id              SERIAL PRIMARY KEY,
    invoice_id      INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
    product         TEXT,
    description     TEXT,
    qty             NUMERIC(14,2),
    unit_price      NUMERIC(14,2),
    discount_pct    NUMERIC(5,2),
    subtotal        NUMERIC(14,2),
    line_order      INTEGER NOT NULL
);

CREATE INDEX idx_invoice_lines_invoice ON invoice_lines(invoice_id);
