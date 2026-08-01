"""stdio MCP server for headless wiki-ingestion sub-agents (`claude -p`).
This is the sub-agent's ONLY interface to the world -- it's invoked with
--tools "" --strict-mcp-config, so it has no Bash/Read/Write/Edit access
at all, just these tools. That's the actual safety boundary, not the
permission system.

Combines:
- All read tools from mcp_server.server, reused directly (search_tickets,
  get_ticket, search_emails, etc.) -- a sub-agent needs to read raw source
  data to write about it.
- New read tools for the wiki inventory across BOTH live (public.wiki_pages)
  and staging (wiki_staging.wiki_pages, this or recent cycles' unpromoted
  drafts) -- collision-avoidance, matching the old wiki-agent's Mapping
  stage insight that a sub-agent needs vault-wide breadth to decide
  CREATE vs UPDATE.
- New write tools, scoped ONLY to wiki_staging -- never public.wiki_pages.

cycle_id and source_kind are fixed per invocation via environment
variables (WIKI_CYCLE_ID, WIKI_SOURCE_KIND), not tool arguments -- each
headless invocation is scoped to exactly one category batch, so there's
no reason to trust (or even let) the model pass these itself.
"""
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

from mcp_server.db import query as db_query, query_one as db_query_one
from mcp_server import server as read_tools
from wiki_ingestion.staging_db import execute_write

CYCLE_ID = int(os.environ["WIKI_CYCLE_ID"])
SOURCE_KIND = os.environ["WIKI_SOURCE_KIND"]

server = Server("wiki-ingestion-agent")


# ---------------------------------------------------------------------------
# Wiki inventory -- live + staging, for collision-avoidance before CREATE.
# ---------------------------------------------------------------------------

def search_wiki_inventory(query):
    """Searches BOTH live wiki_pages and staging drafts (any cycle) by
    title, so a sub-agent can tell whether a page already exists (live)
    or is already being drafted (staging, possibly by an earlier cycle
    not yet promoted) before deciding to CREATE."""
    live = db_query(
        "SELECT id, title, page_type FROM wiki_pages WHERE title ILIKE %s ORDER BY title LIMIT 20",
        (f"%{query}%",),
    )
    staging = db_query(
        """SELECT id, title, page_type, live_page_id, status, cycle_id
           FROM wiki_staging.wiki_pages WHERE title ILIKE %s ORDER BY title LIMIT 20""",
        (f"%{query}%",),
    )
    return {"live_pages": live, "staging_drafts": staging}


def get_live_wiki_page(page_id):
    page = db_query_one("SELECT * FROM wiki_pages WHERE id = %s", (page_id,))
    if not page:
        return {"error": f"no live wiki page with id={page_id}"}
    page.pop("body_tsv", None)
    return page


# ---------------------------------------------------------------------------
# Staging writes -- the only tables this server ever writes to.
# ---------------------------------------------------------------------------

def create_staging_page(title, page_type, body, tags=None, entity_class=None, sources_raw=None):
    """New page (live_page_id NULL -> CREATE on promotion)."""
    row = execute_write(
        """
        INSERT INTO wiki_staging.wiki_pages (
            live_page_id, title, page_type, entity_class, tags, sources_raw,
            body, source_kind, cycle_id, status
        ) VALUES (NULL, %s, %s, %s, %s, %s, %s, %s, %s, 'draft')
        RETURNING id
        """,
        (title, page_type, entity_class, tags or [], sources_raw or [], body, SOURCE_KIND, CYCLE_ID),
    )
    return {"staging_page_id": row["id"]}


def update_staging_page(live_page_id, title, page_type, body, tags=None, entity_class=None, sources_raw=None):
    """Proposes an update to an EXISTING live page -- live_page_id must be
    a real public.wiki_pages id (look it up via search_wiki_inventory
    first). Creates a new staging draft row, does not touch the live row."""
    live = db_query_one("SELECT id FROM wiki_pages WHERE id = %s", (live_page_id,))
    if not live:
        return {"error": f"no live wiki page with id={live_page_id} -- use create_staging_page for a new page"}
    row = execute_write(
        """
        INSERT INTO wiki_staging.wiki_pages (
            live_page_id, title, page_type, entity_class, tags, sources_raw,
            body, source_kind, cycle_id, status
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'draft')
        RETURNING id
        """,
        (live_page_id, title, page_type, entity_class, tags or [], sources_raw or [], body, SOURCE_KIND, CYCLE_ID),
    )
    return {"staging_page_id": row["id"]}


def add_staging_link(staging_page_id, to_title, display_text=None, context_snippet=None):
    draft = db_query_one("SELECT id FROM wiki_staging.wiki_pages WHERE id = %s", (staging_page_id,))
    if not draft:
        return {"error": f"no staging page with id={staging_page_id}"}
    row = execute_write(
        """
        INSERT INTO wiki_staging.wiki_links (from_page_id, to_title_raw, display_text, context_snippet)
        VALUES (%s, %s, %s, %s) RETURNING id
        """,
        (staging_page_id, to_title, display_text, context_snippet),
    )
    return {"link_id": row["id"]}


def add_staging_citation(staging_page_id, source_type, source_id, source_ref_raw):
    draft = db_query_one("SELECT id FROM wiki_staging.wiki_pages WHERE id = %s", (staging_page_id,))
    if not draft:
        return {"error": f"no staging page with id={staging_page_id}"}
    row = execute_write(
        """
        INSERT INTO wiki_staging.wiki_citations (wiki_page_id, source_type, source_id, source_ref_raw)
        VALUES (%s, %s, %s, %s) RETURNING id
        """,
        (staging_page_id, source_type, source_id, source_ref_raw),
    )
    return {"citation_id": row["id"]}


def add_staging_flag(staging_page_id, flag_type, text):
    draft = db_query_one("SELECT id FROM wiki_staging.wiki_pages WHERE id = %s", (staging_page_id,))
    if not draft:
        return {"error": f"no staging page with id={staging_page_id}"}
    row = execute_write(
        """
        INSERT INTO wiki_staging.wiki_flags (wiki_page_id, flag_type, text)
        VALUES (%s, %s, %s) RETURNING id
        """,
        (staging_page_id, flag_type, text),
    )
    return {"flag_id": row["id"]}


WRITE_TOOLS = {
    "search_wiki_inventory": search_wiki_inventory,
    "get_live_wiki_page": get_live_wiki_page,
    "create_staging_page": create_staging_page,
    "update_staging_page": update_staging_page,
    "add_staging_link": add_staging_link,
    "add_staging_citation": add_staging_citation,
    "add_staging_flag": add_staging_flag,
}

# Reuse every read tool from the existing read-only server unchanged.
READ_TOOLS = dict(read_tools.TOOLS)

TOOLS = {**READ_TOOLS, **WRITE_TOOLS}


@server.list_tools()
async def list_tools():
    return [
        Tool(
            name="search_wiki_inventory",
            description="Search BOTH live wiki pages and staging drafts (any cycle) by title -- ALWAYS call "
                        "this before create_staging_page, to check whether a page already exists or is already "
                        "being drafted, so you UPDATE instead of creating a duplicate.",
            inputSchema={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
        ),
        Tool(
            name="get_live_wiki_page",
            description="Return a live (already-promoted) wiki page's full content by id.",
            inputSchema={"type": "object", "properties": {"page_id": {"type": "integer"}}, "required": ["page_id"]},
        ),
        Tool(
            name="create_staging_page",
            description="Create a NEW draft wiki page (only if search_wiki_inventory confirmed nothing existing "
                        "covers this topic). page_type: 'entity'|'concept'|'source'|'analysis'|'overview'|'prospect'.",
            inputSchema={"type": "object", "properties": {
                "title": {"type": "string"}, "page_type": {"type": "string"}, "body": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "entity_class": {"type": "string"},
                "sources_raw": {"type": "array", "items": {"type": "string"}},
            }, "required": ["title", "page_type", "body"]},
        ),
        Tool(
            name="update_staging_page",
            description="Propose an update to an EXISTING live wiki page (live_page_id from search_wiki_inventory's "
                        "live_pages results). Creates a draft, does not modify the live row.",
            inputSchema={"type": "object", "properties": {
                "live_page_id": {"type": "integer"}, "title": {"type": "string"},
                "page_type": {"type": "string"}, "body": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "entity_class": {"type": "string"},
                "sources_raw": {"type": "array", "items": {"type": "string"}},
            }, "required": ["live_page_id", "title", "page_type", "body"]},
        ),
        Tool(
            name="add_staging_link",
            description="Add a [[wikilink]]-equivalent outbound link from a staging page you created/updated "
                        "this call to another page by title (resolved at promotion/consolidation time).",
            inputSchema={"type": "object", "properties": {
                "staging_page_id": {"type": "integer"}, "to_title": {"type": "string"},
                "display_text": {"type": "string"}, "context_snippet": {"type": "string"},
            }, "required": ["staging_page_id", "to_title"]},
        ),
        Tool(
            name="add_staging_citation",
            description="Cite a raw source row on a staging page you created/updated this call. "
                        "source_type: 'email_thread'|'ticket'|'call_transcript'|'implementation_task'.",
            inputSchema={"type": "object", "properties": {
                "staging_page_id": {"type": "integer"}, "source_type": {"type": "string"},
                "source_id": {"type": "integer"}, "source_ref_raw": {"type": "string"},
            }, "required": ["staging_page_id", "source_type", "source_id", "source_ref_raw"]},
        ),
        Tool(
            name="add_staging_flag",
            description="Flag a contradiction or unverified claim on a staging page you created/updated this "
                        "call. flag_type: 'contradiction'|'unverified'.",
            inputSchema={"type": "object", "properties": {
                "staging_page_id": {"type": "integer"}, "flag_type": {"type": "string"}, "text": {"type": "string"},
            }, "required": ["staging_page_id", "flag_type", "text"]},
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
