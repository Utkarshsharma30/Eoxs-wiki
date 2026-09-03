"""Read-only MCP tools for the `repo_docs` table (schema/034_repo_docs.sql)
-- this repository's own docs, ARCHITECTURE.md, and a synthesized codebase
overview, made queryable the same way emails/calls/assets are. Added
2026-08-26.

Every row in this table is tier1 (Raj-only) -- see schema/034's comment for
why. No write tools exist for this table, by explicit instruction: this is
a read-only reference surface, kept in sync by re-running
ingestion/import_repo_docs.py, not edited live through MCP the way
`assets` can be. Tool shape mirrors list_assets/search_assets/get_asset in
mcp_server/server.py exactly -- same clearance-filtering pattern via a
`clearance` kwarg bound at server-construction time, never taken from a
tool call's own arguments.
"""
from mcp.types import Tool

from mcp_server.db import query as db_query, query_one as db_query_one

# Every other tiered tool in mcp_server/server.py defaults its `clearance`
# kwarg to FULL_CLEARANCE (not None) precisely so a caller that never sets
# it explicitly -- like wiki_ingestion/agent_mcp_server.py, which reuses
# server.py's TOOLS dict unwrapped, with no functools.partial/clearance
# binding of its own -- still gets a real access list instead of "no tiers
# at all". These three functions defaulted to None instead, which made
# `access_tier::text = ANY(NULL)` evaluate to NULL (never true) and
# silently returned zero rows to that internal sub-agent for every one of
# the 14 repo_docs rows, every cycle, since this module was added
# 2026-08-26 -- confirmed live 2026-09-03 by re-running the repo_docs
# ingestion batch and watching the sub-agent report all three tools
# returning empty/not-found and correctly refuse to fabricate pages from
# titles alone. The real http_server.py-backed MCP identities were never
# affected (build_server() always binds a real clearance there), only this
# internal synthesis path.
FULL_CLEARANCE = ["tier1", "tier2_confidential_hr", "tier2_confidential", "tier2"]


def list_repo_docs(doc_type="", clearance=FULL_CLEARANCE):
    sql = "SELECT id, slug, title, doc_type, access_tier, updated_at FROM repo_docs WHERE access_tier::text = ANY(%s)"
    params = [clearance]
    if doc_type:
        sql += " AND doc_type = %s"
        params.append(doc_type)
    sql += " ORDER BY doc_type, title"
    return db_query(sql, params)


def search_repo_docs(query, clearance=FULL_CLEARANCE):
    """Trigram-similarity-ranked, same approach as search_assets -- a
    handful of long documents, not worth full-text-search machinery."""
    return db_query(
        """SELECT id, slug, title, doc_type, access_tier,
                  round(LEAST(1.0, similarity(title, %s) + CASE WHEN body ILIKE %s THEN 0.15 ELSE 0 END)::numeric, 3) AS match_score
           FROM repo_docs
           WHERE access_tier::text = ANY(%s)
             AND (similarity(title, %s) > 0.1 OR body ILIKE %s)
           ORDER BY match_score DESC LIMIT 20""",
        (query, f"%{query}%", clearance, query, f"%{query}%"),
    )


def get_repo_doc(identifier, clearance=FULL_CLEARANCE):
    """identifier: the row's numeric id, or its slug (from list_repo_docs/
    search_repo_docs)."""
    if str(identifier).isdigit():
        row = db_query_one("SELECT * FROM repo_docs WHERE id = %s AND access_tier::text = ANY(%s)", (int(identifier), clearance))
    else:
        row = db_query_one("SELECT * FROM repo_docs WHERE slug = %s AND access_tier::text = ANY(%s)", (identifier, clearance))
    if not row:
        return {"error": f"no repo doc matching '{identifier}'"}
    return row


REPO_DOCS_TOOLS = {
    "list_repo_docs": list_repo_docs,
    "search_repo_docs": search_repo_docs,
    "get_repo_doc": get_repo_doc,
}


def tool_defs():
    return [
        Tool(
            name="list_repo_docs",
            description="List this repository's own reference documents (docs/*.md, ARCHITECTURE.md, and a "
                        "synthesized codebase overview) -- tier1 only (Raj/`full` identity). doc_type: "
                        "'doc'|'architecture'|'codebase' or empty for all. Title/id/slug only, not full body -- "
                        "use get_repo_doc for that.",
            inputSchema={"type": "object", "properties": {"doc_type": {"type": "string", "default": ""}}},
        ),
        Tool(
            name="search_repo_docs",
            description="Search this repository's own reference documents by title (fuzzy/approximate match, "
                        "ranked) or body content (substring). Results sorted by 'match_score' (0-1). tier1 only. "
                        "Use list_repo_docs for the full catalog instead.",
            inputSchema={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
        ),
        Tool(
            name="get_repo_doc",
            description="Return the full text of one repository reference document by its numeric 'id' or "
                        "'slug' (from list_repo_docs/search_repo_docs). tier1 only.",
            inputSchema={"type": "object", "properties": {"identifier": {"type": "string"}}, "required": ["identifier"]},
        ),
    ]
