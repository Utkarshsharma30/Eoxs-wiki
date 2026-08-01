"""Runs the full sync pipeline against the vault in dependency order.
Safe to rerun anytime -- every loader is idempotent and skips files whose
mtime hasn't changed since the last run (tracked in db_sync_state).

Order matters: clients must load before tickets/sales_orders/calls (FK),
and wiki must load before citation/link resolution (needs all pages known).
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"

STEPS = [
    ("schema migrations", ["schema/run_migrations.py"]),
    ("clients & contacts", ["loaders/load_clients.py"]),
    ("emails", ["loaders/load_emails.py"]),
    ("tickets", ["loaders/load_tickets.py"]),
    ("invoices", ["loaders/load_invoices.py"]),
    ("calls", ["loaders/load_calls.py"]),
    ("wiki pages + links/flags/citations", ["loaders/load_wiki.py"]),
    ("citation resolution", ["loaders/resolve_citations.py"]),
    ("client_id backfill", ["loaders/resolve_client_ids.py"]),
]


def main():
    for label, args in STEPS:
        print(f"\n=== {label} ===")
        result = subprocess.run([str(PYTHON), *args], cwd=ROOT)
        if result.returncode != 0:
            print(f"\nFAILED at step: {label}")
            sys.exit(1)
    print("\n=== sync complete ===")


if __name__ == "__main__":
    main()
