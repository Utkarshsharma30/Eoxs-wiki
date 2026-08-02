"""stdio MCP server for headless Phase-4 consolidation sub-agents. Same
isolation model as agent_mcp_server.py (no cycle/source_kind env vars
needed here, though -- consolidation never creates a NEW staging row or
attributes one to a cycle, it only merges existing draft rows).

Combines:
- All read tools from mcp_server.server, reused directly -- a consolidation
  sub-agent needs to read raw source data to judge what's genuinely
  overlapping vs genuinely distinct content between duplicate drafts.
- get_staging_page, to read a duplicate group's full content (body +
  citations + flags + links) before merging.
- merge_staging_pages, the only write tool -- reassigns citations/flags/
  links from the duplicate rows onto the surviving row, overwrites the
  surviving row's body/tags/etc with the agent's synthesized merge, then
  deletes the duplicate rows. One transaction, so a mid-merge crash can't
  leave citations reassigned but duplicates not yet deleted (or vice
  versa).
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

from ingestion.db import get_live_conn
from mcp_server.db import query as db_query, query_one as db_query_one
from mcp_server import server as read_tools

server = Server("wiki-consolidation-agent")


def get_staging_page(staging_page_id):
    page = db_query_one("SELECT * FROM wiki_staging.wiki_pages WHERE id = %s", (staging_page_id,))
    if not page:
        return {"error": f"no staging page with id={staging_page_id}"}
    page["citations"] = db_query(
        "SELECT source_type, source_id, source_ref_raw FROM wiki_staging.wiki_citations WHERE wiki_page_id = %s",
        (staging_page_id,),
    )
    page["flags"] = db_query(
        "SELECT flag_type, text FROM wiki_staging.wiki_flags WHERE wiki_page_id = %s", (staging_page_id,)
    )
    page["links"] = db_query(
        "SELECT to_title_raw, display_text, context_snippet FROM wiki_staging.wiki_links WHERE from_page_id = %s",
        (staging_page_id,),
    )
    return page


def merge_staging_pages(keep_id, duplicate_ids, merged_body, tags=None, entity_class=None, sources_raw=None):
    """Merges duplicate_ids into keep_id: reassigns their citations/flags/
    links onto keep_id, overwrites keep_id's content with the caller's
    already-synthesized merge, deletes duplicate_ids. All in one
    transaction. keep_id must not appear in duplicate_ids."""
    if keep_id in duplicate_ids:
        return {"error": "keep_id must not also appear in duplicate_ids"}
    conn = get_live_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM wiki_staging.wiki_pages WHERE id = ANY(%s) AND status = 'draft'",
                ([keep_id] + list(duplicate_ids),),
            )
            found_ids = {r["id"] for r in cur.fetchall()}
            missing = (set([keep_id] + list(duplicate_ids))) - found_ids
            if missing:
                return {"error": f"page id(s) not found or not in draft status: {sorted(missing)}"}

            cur.execute(
                "UPDATE wiki_staging.wiki_citations SET wiki_page_id = %s WHERE wiki_page_id = ANY(%s)",
                (keep_id, list(duplicate_ids)),
            )
            cur.execute(
                "UPDATE wiki_staging.wiki_flags SET wiki_page_id = %s WHERE wiki_page_id = ANY(%s)",
                (keep_id, list(duplicate_ids)),
            )
            cur.execute(
                "UPDATE wiki_staging.wiki_links SET from_page_id = %s WHERE from_page_id = ANY(%s)",
                (keep_id, list(duplicate_ids)),
            )
            cur.execute(
                """
                UPDATE wiki_staging.wiki_pages
                SET body = %s, tags = COALESCE(%s, tags), entity_class = COALESCE(%s, entity_class),
                    sources_raw = COALESCE(%s, sources_raw), updated_at = now()
                WHERE id = %s
                """,
                (merged_body, tags, entity_class, sources_raw, keep_id),
            )
            cur.execute("DELETE FROM wiki_staging.wiki_pages WHERE id = ANY(%s)", (list(duplicate_ids),))
        conn.commit()
        return {"kept_page_id": keep_id, "merged_count": len(duplicate_ids)}
    finally:
        conn.close()


WRITE_TOOLS = {
    "get_staging_page": get_staging_page,
    "merge_staging_pages": merge_staging_pages,
}

READ_TOOLS = dict(read_tools.TOOLS)
TOOLS = {**READ_TOOLS, **WRITE_TOOLS}


@server.list_tools()
async def list_tools():
    return [
        Tool(
            name="get_staging_page",
            description="Read one staging draft's full content (body, tags, entity_class, sources_raw) "
                        "plus its citations, flags, and links, by staging page id.",
            inputSchema={"type": "object", "properties": {
                "staging_page_id": {"type": "integer"},
            }, "required": ["staging_page_id"]},
        ),
        Tool(
            name="merge_staging_pages",
            description="Merge duplicate_ids into keep_id: reassigns their citations/flags/links onto keep_id, "
                        "overwrites keep_id's body/tags/entity_class/sources_raw with your synthesized merge "
                        "(don't just concatenate -- deduplicate overlapping content, keep genuinely distinct "
                        "sub-topics, flag any contradictions you find between the duplicates), then deletes "
                        "duplicate_ids. Call this exactly once per duplicate group.",
            inputSchema={"type": "object", "properties": {
                "keep_id": {"type": "integer"},
                "duplicate_ids": {"type": "array", "items": {"type": "integer"}},
                "merged_body": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "entity_class": {"type": "string"},
                "sources_raw": {"type": "array", "items": {"type": "string"}},
            }, "required": ["keep_id", "duplicate_ids", "merged_body"]},
        ),
    ] + await read_tools.list_tools()


@server.call_tool()
async def call_tool(name, arguments):
    if name not in TOOLS:
        return [TextContent(type="text", text=f"Unknown tool: {name}")]
    func = TOOLS[name]
    try:
        result = func(**(arguments or {}))
    except Exception as e:
        result = {"error": f"{type(e).__name__}: {e}"}
    return [TextContent(type="text", text=json.dumps(result, indent=2, default=str))]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
