"""Applies frontend_threads/schema/*.sql against the eoxs_frontend_threads
database, same simple pattern as schema/run_migrations.py. Run once, after
the database itself has been created (needs a superuser -- see the
CREATE DATABASE command in the accompanying setup notes)."""
import os
from pathlib import Path

import psycopg2
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")

SCHEMA_DIR = Path(__file__).resolve().parent


def get_conn():
    return psycopg2.connect(
        host=os.environ["PGHOST"], port=os.environ["PGPORT"], dbname="eoxs_frontend_threads",
        user=os.environ["PGUSER"], password=os.environ["PGPASSWORD"],
    )


def main():
    conn = get_conn()
    conn.autocommit = False
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                filename TEXT PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        conn.commit()

    for sql_file in sorted(SCHEMA_DIR.glob("*.sql")):
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM schema_migrations WHERE filename = %s", (sql_file.name,))
            if cur.fetchone():
                print(f"skip  {sql_file.name} (already applied)")
                continue
            cur.execute(sql_file.read_text())
            cur.execute("INSERT INTO schema_migrations (filename) VALUES (%s)", (sql_file.name,))
        conn.commit()
        print(f"applied {sql_file.name}")
    conn.close()


if __name__ == "__main__":
    main()
