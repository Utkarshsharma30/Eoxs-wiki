"""Loads raw/invoices/*.md into sales_orders / order_lines.
Idempotent: upserts on order_number; skips unchanged files via db_sync_state.
"""
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loaders._db import get_conn, VAULT_ROOT
from parsers.frontmatter import parse_frontmatter
from parsers.invoice_body import extract_order_lines

SOURCE_TYPE = "sales_order"


def to_date(value):
    if not value:
        return None
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def to_dt(value):
    if not value:
        return None
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


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
    if not meta.get("order_number"):
        return "no_order_number"

    lines = extract_order_lines(body)

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO sales_orders (
                odoo_id, order_number, client_raw, order_date, expiry_date,
                amount_total, currency, state, state_label, salesperson,
                linked_invoices, generated_at, source_file_path, source_file_mtime
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (order_number) DO UPDATE SET
                odoo_id = EXCLUDED.odoo_id,
                client_raw = EXCLUDED.client_raw,
                order_date = EXCLUDED.order_date,
                expiry_date = EXCLUDED.expiry_date,
                amount_total = EXCLUDED.amount_total,
                currency = EXCLUDED.currency,
                state = EXCLUDED.state,
                state_label = EXCLUDED.state_label,
                salesperson = EXCLUDED.salesperson,
                linked_invoices = EXCLUDED.linked_invoices,
                generated_at = EXCLUDED.generated_at,
                source_file_path = EXCLUDED.source_file_path,
                source_file_mtime = EXCLUDED.source_file_mtime,
                updated_at = now()
            RETURNING id
            """,
            (
                meta.get("odoo_id"), meta["order_number"], meta.get("client"),
                to_date(meta.get("date")), to_date(meta.get("expiry_date")),
                meta.get("amount_total"), meta.get("currency"), meta.get("state"),
                meta.get("state_label"), meta.get("salesperson"),
                [str(x) for x in (meta.get("linked_invoices") or [])],
                to_dt(meta.get("generated_at")), rel_path, mtime,
            ),
        )
        order_id = cur.fetchone()[0]

        cur.execute("DELETE FROM order_lines WHERE sales_order_id = %s", (order_id,))
        for line in lines:
            cur.execute(
                """
                INSERT INTO order_lines (
                    sales_order_id, product, description, qty, delivered,
                    invoiced, unit_price, discount_pct, subtotal, line_order
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    order_id, line["product"], line["description"], line["qty"],
                    line["delivered"], line["invoiced"], line["unit_price"],
                    line["discount_pct"], line["subtotal"], line["line_order"],
                ),
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
    root = VAULT_ROOT / "raw/invoices"
    counts = {"loaded": 0, "skipped": 0, "no_order_number": 0, "error": 0}
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
