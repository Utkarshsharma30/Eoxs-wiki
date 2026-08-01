"""Parses a sales-order body's '## Order Lines' markdown table."""
import re

ORDER_LINES_RE = re.compile(r"## Order Lines\s*\n\|.*?\|\n\|[-\s|]+\|\n(.*?)(?=\n\*\*Total|\n##|\Z)", re.DOTALL)

MONEY_RE = re.compile(r"[\$,]")


def to_number(s):
    s = s.strip()
    if not s or s == "-":
        return None
    s = MONEY_RE.sub("", s).replace("%", "")
    try:
        return float(s)
    except ValueError:
        return None


def extract_order_lines(body):
    """Returns a list of dicts matching the 'Product | Description | Qty | Delivered |
    Invoiced | Unit Price | Disc% | Subtotal' table columns."""
    m = ORDER_LINES_RE.search(body)
    if not m:
        return []

    lines = []
    for i, row in enumerate(m.group(1).strip().split("\n")):
        row = row.strip()
        if not row.startswith("|"):
            continue
        cells = [c.strip() for c in row.strip("|").split("|")]
        if len(cells) < 8:
            continue
        lines.append({
            "product": cells[0] or None,
            "description": cells[1] or None,
            "qty": to_number(cells[2]),
            "delivered": to_number(cells[3]),
            "invoiced": to_number(cells[4]),
            "unit_price": to_number(cells[5]),
            "discount_pct": to_number(cells[6]),
            "subtotal": to_number(cells[7]),
            "line_order": i,
        })
    return lines
