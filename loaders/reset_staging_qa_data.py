"""One-command reset for the `staging_qa` MCP identity's sandbox
(mcp_server/http_server.py) -- wipes whatever QA testing left behind in
`eoxs_wiki_staging`'s `employees`/`assets` tables and re-mirrors both,
row for row (including `id`), from live `eoxs_wiki`. Run this between QA
sessions to get back to a known-good baseline instead of trying to hand-
revert individual test writes.

Never touches live -- only ever reads from it (get_live_conn) and writes to
staging (get_staging_conn), the same two connections ingestion/db.py's
dual_write() already uses elsewhere in this codebase.

Why a full wipe+remirror instead of replaying employee_change_log/
asset_change_log's old/new diffs to "undo" each change: a wipe+remirror is
correct regardless of what QA did -- brand-new fabricated rows, edited
existing rows, anything in between -- with no risk of a diff-replay bug
leaving a stray row behind. The change logs remain valuable for something
different: seeing exactly what a given QA session actually wrote (`SELECT *
FROM employee_change_log WHERE changed_by='staging_qa'` against staging),
not for mechanically undoing it.

Usage:
  python -m loaders.reset_staging_qa_data              # dry-run, shows counts only
  python -m loaders.reset_staging_qa_data --commit      # actually wipes + remirrors
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ingestion.db import get_live_conn, get_staging_conn, LIVE_DB, STAGING_DB

EMPLOYEE_COLUMNS = (
    "id, full_name, department, role_title, employment_type, official_email, "
    "manager, date_of_joining, date_of_leaving, status, notes, created_at, updated_at"
)
ASSET_COLUMNS = "id, slug, title, body, source_file_path, access_tier, imported_at, updated_at"


def _fetch_live(cur_sql):
    conn = get_live_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(cur_sql)
            return cur.fetchall()
    finally:
        conn.close()


def reset(dry_run=True):
    assert LIVE_DB != STAGING_DB, "refusing to run: live and staging resolved to the same database"

    live_employees = _fetch_live(f"SELECT {EMPLOYEE_COLUMNS} FROM employees ORDER BY id")
    live_assets = _fetch_live(f"SELECT {ASSET_COLUMNS} FROM assets ORDER BY id")

    print(f"live ({LIVE_DB}): {len(live_employees)} employees, {len(live_assets)} assets")

    if dry_run:
        print(f"[dry-run] would wipe staging ({STAGING_DB})'s employee_change_log/employees/"
              f"asset_change_log/assets, then re-insert {len(live_employees)} employees + "
              f"{len(live_assets)} assets from live. Pass --commit to actually do this.")
        return

    conn = get_staging_conn()
    try:
        with conn.cursor() as cur:
            # CASCADE handles FK order (employee_change_log -> employees,
            # asset_change_log -> assets) in one statement each; RESTART
            # IDENTITY so a fresh test row created after this starts from a
            # clean sequence rather than continuing from wherever QA left off.
            cur.execute("TRUNCATE employee_change_log, employees RESTART IDENTITY CASCADE")
            cur.execute("TRUNCATE asset_change_log, assets RESTART IDENTITY CASCADE")

            for row in live_employees:
                cur.execute(
                    """INSERT INTO employees (id, full_name, department, role_title, employment_type,
                                               official_email, manager, date_of_joining, date_of_leaving,
                                               status, notes, created_at, updated_at)
                       VALUES (%(id)s, %(full_name)s, %(department)s, %(role_title)s, %(employment_type)s,
                               %(official_email)s, %(manager)s, %(date_of_joining)s, %(date_of_leaving)s,
                               %(status)s, %(notes)s, %(created_at)s, %(updated_at)s)""",
                    row,
                )
            for row in live_assets:
                cur.execute(
                    """INSERT INTO assets (id, slug, title, body, source_file_path, access_tier,
                                            imported_at, updated_at)
                       VALUES (%(id)s, %(slug)s, %(title)s, %(body)s, %(source_file_path)s, %(access_tier)s,
                               %(imported_at)s, %(updated_at)s)""",
                    row,
                )

            if live_employees:
                cur.execute("SELECT setval('employees_id_seq', (SELECT max(id) FROM employees))")
            if live_assets:
                cur.execute("SELECT setval('assets_id_seq', (SELECT max(id) FROM assets))")
        conn.commit()
        print(f"staging ({STAGING_DB}) reset: {len(live_employees)} employees + "
              f"{len(live_assets)} assets re-mirrored from live. Change logs cleared.")
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--commit", action="store_true")
    args = parser.parse_args()
    reset(dry_run=not args.commit)


if __name__ == "__main__":
    main()
