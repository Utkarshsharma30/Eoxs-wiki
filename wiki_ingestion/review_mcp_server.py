"""stdio MCP server for headless Phase-5 review sub-agents. Quality gate
between consolidation (Phase 4) and promotion: a draft only becomes
eligible for promotion after this marks it 'reviewed'. Promotion itself
(wiki_ingestion/promote.py) is a separate, currently human-gated step --
this server has no promotion tool at all, on purpose, so a review
sub-agent cannot accidentally push anything live.

Combines:
- All read tools from mcp_server.server -- spot-checking a draft's
  citations against the actual raw source rows is the main thing this
  stage does that Phase 3/4 didn't already do.
- get_staging_page (same as consolidate_mcp_server.py) for the draft's
  own content.
- mark_reviewed / mark_rejected, the only write tools -- both require
  status='draft' going in (a page already reviewed/rejected/promoted
  can't be re-judged by a stray tool call).
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

server = Server("wiki-review-agent")


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


def _set_status(staging_page_id, new_status, notes):
    conn = get_live_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE wiki_staging.wiki_pages SET status = %s, review_notes = %s, updated_at = now()
                WHERE id = %s AND status = 'draft'
                RETURNING id
                """,
                (new_status, notes, staging_page_id),
            )
            row = cur.fetchone()
        conn.commit()
        if not row:
            return {"error": f"no draft-status staging page with id={staging_page_id} (already reviewed, or doesn't exist)"}
        return {"staging_page_id": staging_page_id, "status": new_status}
    finally:
        conn.close()


def mark_reviewed(staging_page_id, notes=None):
    return _set_status(staging_page_id, "reviewed", notes)


def mark_rejected(staging_page_id, reason):
    return _set_status(staging_page_id, "rejected", reason)


WRITE_TOOLS = {
    "get_staging_page": get_staging_page,
    "mark_reviewed": mark_reviewed,
    "mark_rejected": mark_rejected,
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
            name="mark_reviewed",
            description="Approve a draft for promotion -- call after verifying its citations against the "
                        "actual raw source rows (using the read tools) and judging it genuinely useful, "
                        "well-formed content (not noise). Only works on a draft-status page.",
            inputSchema={"type": "object", "properties": {
                "staging_page_id": {"type": "integer"}, "notes": {"type": "string"},
            }, "required": ["staging_page_id"]},
        ),
        Tool(
            name="mark_rejected",
            description="Reject a draft -- call when citations don't check out, content is noise/near-empty, "
                        "or it's otherwise not worth promoting. reason is required and should be specific "
                        "enough that someone reading it later understands why. Only works on a draft-status page.",
            inputSchema={"type": "object", "properties": {
                "staging_page_id": {"type": "integer"}, "reason": {"type": "string"},
            }, "required": ["staging_page_id", "reason"]},
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
