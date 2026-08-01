"""Loads clients + contacts from tools/contact_registry.yaml (authoritative
8-client list with contacts/domains) and tools/config.yaml (odoo_base_url/db
for the 6 clients that have live Odoo implementation tracking).

Must run before load_tickets/load_invoices/load_calls, since those FK-reference
clients.id. Idempotent: upserts on clients.slug.
"""
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from loaders._db import get_conn, VAULT_ROOT


def load_yaml(rel_path):
    path = VAULT_ROOT / rel_path
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def main():
    conn = get_conn()

    registry = load_yaml("tools/contact_registry.yaml")
    config = load_yaml("tools/config.yaml")

    odoo_by_slug = {}
    for c in (config.get("odoo_implementation", {}) or {}).get("clients", []):
        # config.yaml ids use short codes (e.g. 'ess', '3gm') that map to the
        # contact_registry slugs by display-name/vault_path convention.
        slug = Path(c["vault_path"]).parts[1]  # raw/clients/<slug>/implementation
        odoo_by_slug[slug] = {"base_url": c.get("base_url"), "db": c.get("db")}

    client_ids = {}
    with conn.cursor() as cur:
        for slug, info in registry.get("clients", {}).items():
            odoo = odoo_by_slug.get(slug, {})
            cur.execute(
                """
                INSERT INTO clients (slug, display_name, domains, odoo_base_url, odoo_db)
                VALUES (%s,%s,%s,%s,%s)
                ON CONFLICT (slug) DO UPDATE SET
                    display_name = EXCLUDED.display_name,
                    domains = EXCLUDED.domains,
                    odoo_base_url = EXCLUDED.odoo_base_url,
                    odoo_db = EXCLUDED.odoo_db,
                    updated_at = now()
                RETURNING id
                """,
                (
                    slug, info.get("display_name"),
                    [str(d) for d in (info.get("domains") or [])],
                    odoo.get("base_url"), odoo.get("db"),
                ),
            )
            client_ids[slug] = cur.fetchone()[0]
    conn.commit()

    contact_count = 0
    with conn.cursor() as cur:
        # contacts have no natural key in the source YAML; re-derive fully each
        # run rather than risk duplicate rows on rerun.
        cur.execute("DELETE FROM contacts WHERE client_id = ANY(%s)", (list(client_ids.values()),))
        for slug, info in registry.get("clients", {}).items():
            client_id = client_ids[slug]
            for contact in info.get("contacts", []):
                name = contact.get("name")
                email = contact.get("email")
                is_relay = "relay inbox" in (name or "").lower()
                cur.execute(
                    """
                    INSERT INTO contacts (client_id, name, email, is_relay_inbox)
                    VALUES (%s,%s,%s,%s)
                    """,
                    (client_id, name, email, is_relay),
                )
                contact_count += 1
    conn.commit()

    conn.close()
    print(f"clients: {len(client_ids)}")
    print(f"contacts: {contact_count}")


if __name__ == "__main__":
    main()
