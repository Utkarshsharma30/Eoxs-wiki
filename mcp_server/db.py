"""DB connection helper for the MCP server. Read-only for every existing
table -- execute() below is the one exception, added specifically for the
employees table (mcp_server/employees.py), the first write path this MCP
server has ever had. Every other tool in this codebase only ever calls
query()/query_one()."""
import os
from pathlib import Path

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")


def get_conn():
    return psycopg2.connect(
        host=os.environ["PGHOST"],
        port=os.environ["PGPORT"],
        dbname=os.environ["PGDATABASE"],
        user=os.environ["PGUSER"],
        password=os.environ["PGPASSWORD"],
        cursor_factory=psycopg2.extras.RealDictCursor,
    )


def query(sql, params=None):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
            return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()


def query_one(sql, params=None):
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql, params=None):
    """Runs an INSERT/UPDATE with a RETURNING clause and commits. Returns the
    single returned row as a dict, or None if RETURNING produced no row."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
            row = cur.fetchone()
        conn.commit()
        return dict(row) if row else None
    finally:
        conn.close()
