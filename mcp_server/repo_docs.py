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


def list_repo_docs(doc_type="", clearance=None):
    sql = "SELECT id, slug, title, doc_type, access_tier, updated_at FROM repo_docs WHERE access_tier::text = ANY(%s)"
    params = [clearance]
    if doc_type:
        sql += " AND doc_type = %s"
        params.append(doc_type)
    sql += " ORDER BY doc_type, title"
    return db_query(sql, params)


def search_repo_docs(query, clearance=None):
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


def get_repo_doc(identifier, clearance=None):
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
