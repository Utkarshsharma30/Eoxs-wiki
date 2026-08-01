"""Write helper for wiki_staging, separate from mcp_server/db.py on
purpose: that module is documented and used as read-only (its query()
never commits, which is correct for its contract -- the public-facing
read-only MCP connector's safety boundary partly rests on that file only
ever running SELECTs). Reusing it for INSERTs here would have silently
rolled back every write (found exactly this bug while testing: query_one()
on an INSERT...RETURNING reported a fresh id, but the row was gone by the
next connection since nothing ever committed).
"""
from ingestion.db import get_live_conn


def execute_write(sql, params=None):
    """Runs one INSERT/UPDATE/DELETE against eoxs_wiki (the same database
    wiki_staging lives in -- see schema/017's comment on why staging is a
    schema, not a separate database). Commits before returning. Returns
    the first row as a dict if the statement has a RETURNING clause,
    else None."""
    conn = get_live_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
            row = None
            if cur.description:
                row = cur.fetchone()
        conn.commit()
        return dict(row) if row else None
    finally:
        conn.close()
