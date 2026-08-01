"""Backfills client_id on tickets, sales_orders, and email_threads by
matching their freetext client/participant fields against the clients
table. client_raw values are raw CRM/Odoo customer names, not clean slugs
(e.g. 'Greer Steel Company', 'PPC Speciality Metals.', but also unrelated
values like 'Rajat Jain', 'Primrose Alloys' (a prospect, not a client), or
'lxbfYeaa' (junk/test data)) -- so this uses an explicit alias map rather
than fuzzy matching, and leaves genuinely non-client rows unmatched rather
than risk a wrong assignment.

Safe to rerun: always fully re-derives client_id from current alias rules.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from loaders._db import get_conn

# client slug -> known aliases seen in raw client_raw / participant text
# (lowercase, matched via ILIKE '%alias%' against the raw value)
ALIASES = {
    "sabre-alloys": ["sabre alloys", "manitex sabre", "sabrealloys.com"],
    "3gm-steel": ["3gm steel", "3gmsteel.com"],
    "eastern-states-steel": ["eastern states steel", "eastern steel sales", "easternstatessteel.com"],
    "discount-pipe-steel": ["discount pipe", "discountpipesteel.com"],
    "ppc-metals": ["ppc metals", "ppc speciality metals", "ppc specialty metals", "ppcmetals.com"],
    "greer-steel": ["greer steel", "ohio strip steel", "greersteel.com", "ohiostripsteel.com"],
    "rw-conklin-steel": ["r w conklin", "rw conklin", "conklinsteel.com"],
    "brannon-steel": ["brannon steel", "brannonsteel.com"],
}


def load_client_ids(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT id, slug FROM clients")
        return {slug: cid for cid, slug in cur.fetchall()}


def backfill_table(conn, table, raw_column, client_ids):
    updated = 0
    with conn.cursor() as cur:
        for slug, aliases in ALIASES.items():
            client_id = client_ids[slug]
            for alias in aliases:
                cur.execute(
                    f"""
                    UPDATE {table}
                    SET client_id = %s
                    WHERE {raw_column} ILIKE %s
                      AND (client_id IS NULL OR client_id != %s)
                    """,
                    (client_id, f"%{alias}%", client_id),
                )
                updated += cur.rowcount
    conn.commit()
    return updated


def backfill_email_threads(conn, client_ids):
    """email_threads has no client_raw column -- match via participants'
    email domains against clients.domains instead."""
    updated = 0
    with conn.cursor() as cur:
        cur.execute("SELECT id, domains FROM clients")
        for client_id, domains in cur.fetchall():
            for domain in domains:
                cur.execute(
                    """
                    UPDATE email_threads
                    SET client_id = %s
                    WHERE EXISTS (
                        SELECT 1 FROM unnest(participants) p WHERE p ILIKE %s
                    )
                    AND (client_id IS NULL OR client_id != %s)
                    """,
                    (client_id, f"%@{domain}", client_id),
                )
                updated += cur.rowcount
    conn.commit()
    return updated


def main():
    conn = get_conn()
    client_ids = load_client_ids(conn)

    t_updated = backfill_table(conn, "tickets", "client_raw", client_ids)
    print(f"tickets: {t_updated} rows updated")

    o_updated = backfill_table(conn, "sales_orders", "client_raw", client_ids)
    print(f"sales_orders: {o_updated} rows updated")

    e_updated = backfill_email_threads(conn, client_ids)
    print(f"email_threads: {e_updated} rows updated")

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM tickets WHERE client_id IS NULL")
        print(f"tickets still unmatched: {cur.fetchone()[0]}")
        cur.execute("SELECT count(*) FROM sales_orders WHERE client_id IS NULL")
        print(f"sales_orders still unmatched: {cur.fetchone()[0]}")
        cur.execute("SELECT count(*) FROM email_threads WHERE client_id IS NULL")
        print(f"email_threads still unmatched: {cur.fetchone()[0]}")

    conn.close()


if __name__ == "__main__":
    main()
