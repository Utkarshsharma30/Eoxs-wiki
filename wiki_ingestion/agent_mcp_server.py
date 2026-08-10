"""MCP server for headless wiki-ingestion sub-agents (`claude -p`). This
is the sub-agent's ONLY interface to the world -- it's invoked with
--strict-mcp-config and a short built-in-tool deny-list, so it has no
Bash/Read/Write/Edit access at all, just these tools. That's the actual
safety boundary, not the permission system.

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

cycle_id and source_kind are fixed per invocation, not tool arguments --
each headless invocation is scoped to exactly one category batch, so
there's no reason to trust (or even let) the model pass these itself.

2026-08 transport change: this used to be a stdio server, freshly
spawned as a subprocess by claude -p on every single invocation, with
cycle_id/source_kind read from WIKI_CYCLE_ID/WIKI_SOURCE_KIND env vars
set once at process start. That connection was found to be unreliable
(see wiki_ingestion/headless_agent.py's incident note) -- claude -p's
own stdio MCP handshake to a brand-new subprocess sometimes never
completed, even though the server itself answered a raw handshake
correctly and instantly in isolation. Root-level fix: build_agent_server
below is now a FACTORY (cycle_id/source_kind passed as real arguments,
not read from the environment), so wiki_ingestion/mcp_http_server.py can
build a fresh, correctly-scoped Server instance per SSE connection
against one always-on, already-proven-reliable persistent HTTP server
(the same pattern mcp_server/http_server.py already uses for the
external claude.ai connector) instead of spawning a subprocess per call.
The __main__ block below still supports running this as a plain stdio
server directly (env-var-driven) for local debugging.
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


TOOL_DEFS = [
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
                        "source_type: 'email_thread'|'ticket'|'call_transcript'|'implementation_task'. "
                        "For 'implementation_task', source_id MUST be odoo_task_id, never the internal `id` "
                        "field (get_implementation_task's response includes both -- `id` is unstable across "
                        "raw-ingestion refreshes and a citation using it can go stale within hours).",
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
]


def build_agent_server(cycle_id, source_kind):
    """Builds a fresh Server instance scoped to one (cycle_id, source_kind)
    pair -- call once per connection/invocation, never share across two
    different batches (create_staging_page/update_staging_page below
    close over these two values directly, matching the old env-var-driven
    behavior but as real closures instead of process-global state)."""

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
            (title, page_type, entity_class, tags or [], sources_raw or [], body, source_kind, cycle_id),
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
            (live_page_id, title, page_type, entity_class, tags or [], sources_raw or [], body, source_kind, cycle_id),
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

    write_tools = {
        "search_wiki_inventory": search_wiki_inventory,
        "get_live_wiki_page": get_live_wiki_page,
        "create_staging_page": create_staging_page,
        "update_staging_page": update_staging_page,
        "add_staging_link": add_staging_link,
        "add_staging_citation": add_staging_citation,
        "add_staging_flag": add_staging_flag,
    }
    # Reuse every read tool from the existing read-only server unchanged.
    tools = {**dict(read_tools.TOOLS), **write_tools}

    srv = Server(f"wiki-ingestion-agent-{cycle_id}-{source_kind}")

    @srv.list_tools()
    async def list_tools():
        return TOOL_DEFS + read_tools._tool_defs()

    @srv.call_tool()
    async def call_tool(name, arguments):
        if name not in tools:
            return [TextContent(type="text", text=f"Unknown tool: {name}")]
        func = tools[name]
        try:
            result = func(**(arguments or {}))
        except Exception as e:
            result = {"error": f"{type(e).__name__}: {e}"}
        return [TextContent(type="text", text=json.dumps(result, indent=2, default=str))]

    return srv


async def _main():
    """Local stdio debugging entrypoint -- reads WIKI_CYCLE_ID/WIKI_SOURCE_KIND
    from the environment, matching the old always-stdio behavior. Production
    now runs via wiki_ingestion/mcp_http_server.py instead (see module
    docstring)."""
    cycle_id = int(os.environ["WIKI_CYCLE_ID"])
    source_kind = os.environ["WIKI_SOURCE_KIND"]
    srv = build_agent_server(cycle_id, source_kind)
    async with stdio_server() as (read_stream, write_stream):
        await srv.run(read_stream, write_stream, srv.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(_main())
