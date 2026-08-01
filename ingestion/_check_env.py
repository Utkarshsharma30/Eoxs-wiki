"""One-time diagnostic: confirms every required env var name is present in
.env (values never printed). Run from the eoxs-wiki-db project root."""
import os
from pathlib import Path

from dotenv import dotenv_values

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"

REQUIRED = [
    "PGHOST", "PGPORT", "PGDATABASE", "PGUSER", "PGPASSWORD",
    "LINEAR_API_KEY", "LINEAR_TEAM_KEY",
    "ANTHROPIC_API_KEY",
    "RAJ_GMAIL_CLIENT_ID", "RAJ_GMAIL_CLIENT_SECRET", "RAJ_GMAIL_REFRESH_TOKEN",
    "RON_GMAIL_CLIENT_ID", "RON_GMAIL_CLIENT_SECRET", "RON_GMAIL_REFRESH_TOKEN",
    "REMYA_GMAIL_CLIENT_ID", "REMYA_GMAIL_CLIENT_SECRET", "REMYA_GMAIL_REFRESH_TOKEN",
    "ZOHO_CLIENT_ID", "ZOHO_CLIENT_SECRET", "ZOHO_REFRESH_TOKEN",
    "FIREFLIES_API_KEY",
    "RON_FATHOM_API_KEY",
    "GREER_ODOO_USERNAME", "GREER_ODOO_PASSWORD",
    "ESS_ODOO_USERNAME", "ESS_ODOO_PASSWORD",
    "DPS_ODOO_USERNAME", "DPS_ODOO_PASSWORD",
    "PPC_ODOO_USERNAME", "PPC_ODOO_PASSWORD",
    "THREEGM_ODOO_USERNAME", "THREEGM_ODOO_PASSWORD",
    "SABRE_ODOO_USERNAME", "SABRE_ODOO_PASSWORD",
]

values = dotenv_values(ENV_PATH)
present = {k for k, v in values.items() if v}  # empty-string values count as missing
missing = [v for v in REQUIRED if v not in present]

print(f".env path: {ENV_PATH}")
print(f"total vars in file: {len(values)}")
print(f"required: {len(REQUIRED)}, present-and-nonempty: {len(REQUIRED) - len(missing)}")
if missing:
    print("MISSING or EMPTY:")
    for m in missing:
        print(f"  - {m}")
else:
    print("ALL REQUIRED VARS PRESENT")
