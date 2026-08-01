-- Odoo sales orders / invoices. Source: raw/invoices/*.md

CREATE TABLE sales_orders (
    id                  SERIAL PRIMARY KEY,
    odoo_id             INTEGER,
    order_number        TEXT NOT NULL UNIQUE,   -- e.g. 'S00003'
    client_raw          TEXT,
    client_id           INTEGER REFERENCES clients(id) ON DELETE SET NULL,
    order_date          DATE,
    expiry_date         DATE,
    amount_total        NUMERIC(14,2),
    currency            TEXT,
    state               TEXT,
    state_label         TEXT,
    salesperson         TEXT,
    linked_invoices     TEXT[] NOT NULL DEFAULT '{}',
    generated_at        TIMESTAMPTZ,
    source_file_path    TEXT NOT NULL UNIQUE,
    source_file_mtime   TIMESTAMPTZ NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_sales_orders_client_id ON sales_orders(client_id);

CREATE TABLE order_lines (
    id              SERIAL PRIMARY KEY,
    sales_order_id  INTEGER NOT NULL REFERENCES sales_orders(id) ON DELETE CASCADE,
    product         TEXT,
    description     TEXT,
    qty             NUMERIC(14,2),
    delivered       NUMERIC(14,2),
    invoiced        NUMERIC(14,2),
    unit_price      NUMERIC(14,2),
    discount_pct    NUMERIC(5,2),
    subtotal        NUMERIC(14,2),
    line_order      INTEGER NOT NULL
);

CREATE INDEX idx_order_lines_sales_order_id ON order_lines(sales_order_id);
