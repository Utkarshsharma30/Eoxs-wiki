"""MCP server exposing eoxs_wiki (Postgres) via a tool set mirroring the
existing OV2 vault MCP server, for direct reliability/quality comparison.

Read-only: every tool issues SELECT queries against eoxs_app, which itself
has no reason to hold write privileges beyond what the loaders need locally.

Run with: python mcp_server/server.py
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

from mcp_server.db import query as db_query, query_one as db_query_one

server = Server("eoxs-wiki-db")

BODY_PREVIEW_CHARS = 1500  # full body is often 10-50K chars; a preview keeps get_* calls usable

# ---------------------------------------------------------------------------
# Tool implementations. Every function's parameter names match the JSON
# schema keys below exactly -- call_tool() dispatches via func(**arguments),
# so a mismatch here silently breaks every real call from Claude.
# ---------------------------------------------------------------------------

def get_index():
    counts = db_query_one("""
        SELECT
            (SELECT count(*) FROM wiki_pages) AS wiki_pages,
            (SELECT count(*) FROM email_threads) AS email_threads,
            (SELECT count(*) FROM tickets) AS tickets,
            (SELECT count(*) FROM sales_orders) AS sales_orders,
            (SELECT count(*) FROM call_transcripts WHERE source='fireflies') AS fireflies_calls,
            (SELECT count(*) FROM call_transcripts WHERE source='fathom') AS fathom_calls,
            (SELECT count(*) FROM clients) AS clients
    """)
    by_type = db_query("SELECT page_type, count(*) AS n FROM wiki_pages GROUP BY page_type ORDER BY page_type")
    return {"totals": counts, "wiki_pages_by_type": by_type}


def get_wiki_page(title):
    page = db_query_one(
        "SELECT * FROM wiki_pages WHERE title ILIKE %s ORDER BY updated_date DESC NULLS LAST LIMIT 1",
        (f"%{title}%",),
    )
    if not page:
        return {"error": f"no wiki page matching '{title}'"}

    full_body = page.pop("body", "") or ""
    page["body_preview"] = full_body[:BODY_PREVIEW_CHARS]
    page["body_length"] = len(full_body)
    page["body_truncated"] = len(full_body) > BODY_PREVIEW_CHARS
    page.pop("body_tsv", None)

    page["outbound_links"] = db_query(
        """SELECT wl.to_title_raw, wp.title AS resolved_title
           FROM wiki_links wl LEFT JOIN wiki_pages wp ON wp.id = wl.to_page_id
           WHERE wl.from_page_id = %s""",
        (page["id"],),
    )
    page["flags"] = db_query(
        "SELECT flag_type, text FROM wiki_flags WHERE wiki_page_id = %s", (page["id"],)
    )
    return page


def search_wiki(query):
    return db_query(
        """SELECT title, page_type, ts_headline('english', body, plainto_tsquery('english', %s)) AS snippet
           FROM wiki_pages
           WHERE body_tsv @@ plainto_tsquery('english', %s)
           ORDER BY ts_rank(body_tsv, plainto_tsquery('english', %s)) DESC
           LIMIT 20""",
        (query, query, query),
    )


def list_emails(account="all", month=""):
    sql = "SELECT id, source_account, gmail_thread_id, subject, message_count, source_file_path FROM email_threads WHERE 1=1"
    params = []
    if account != "all":
        sql += " AND source_account = %s"
        params.append(account)
    if month:
        sql += " AND source_file_path LIKE %s"
        params.append(f"%/{month}/%")
    sql += " ORDER BY source_file_path DESC LIMIT 100"
    return db_query(sql, params)


def search_emails(query, account="all"):
    sql = """
        SELECT DISTINCT t.id, t.source_account, t.subject, t.source_file_path,
               ts_headline('english', m.body, plainto_tsquery('english', %s)) AS snippet
        FROM email_threads t JOIN email_messages m ON m.thread_id = t.id
        WHERE m.body_tsv @@ plainto_tsquery('english', %s)
    """
    params = [query, query]
    if account != "all":
        sql += " AND t.source_account = %s"
        params.append(account)
    sql += " LIMIT 20"
    return db_query(sql, params)


def get_email(file_path):
    thread = db_query_one("SELECT * FROM email_threads WHERE source_file_path = %s", (file_path,))
    if not thread:
        return {"error": f"no email thread at '{file_path}'"}
    thread["messages"] = db_query(
        "SELECT message_index, message_date, from_addr, body FROM email_messages WHERE thread_id = %s ORDER BY message_index",
        (thread["id"],),
    )
    return thread


def list_calls(month="", source=""):
    sql = "SELECT id, source, meeting_title, call_date, participants, source_file_path FROM call_transcripts WHERE 1=1"
    params = []
    if source:
        sql += " AND source = %s"
        params.append(source)
    if month:
        sql += " AND to_char(call_date, 'YYYY-MM') = %s"
        params.append(month)
    sql += " ORDER BY call_date DESC NULLS LAST LIMIT 100"
    return db_query(sql, params)


def search_calls(query, source=""):
    sql = """
        SELECT id, source, meeting_title, call_date, source_file_path,
               ts_headline('english', transcript_body, plainto_tsquery('english', %s)) AS snippet
        FROM call_transcripts
        WHERE transcript_tsv @@ plainto_tsquery('english', %s)
    """
    params = [query, query]
    if source:
        sql += " AND source = %s"
        params.append(source)
    sql += " ORDER BY call_date DESC NULLS LAST LIMIT 20"
    return db_query(sql, params)


def get_call(file_path):
    call = db_query_one("SELECT * FROM call_transcripts WHERE source_file_path = %s", (file_path,))
    if not call:
        return {"error": f"no call at '{file_path}'"}
    call["segments"] = db_query(
        "SELECT segment_order, speaker, text FROM call_segments WHERE call_id = %s ORDER BY segment_order",
        (call["id"],),
    )
    return call


def get_ticket(identifier):
    ticket = db_query_one("SELECT * FROM tickets WHERE ticket_number = %s", (identifier.upper(),))
    if not ticket:
        ticket = db_query_one("SELECT * FROM tickets WHERE subject ILIKE %s LIMIT 1", (f"%{identifier}%",))
    if not ticket:
        return {"error": f"no ticket matching '{identifier}'"}
    ticket["events"] = db_query(
        "SELECT event_order, event_type, author, event_time, body FROM ticket_events WHERE ticket_id = %s ORDER BY event_order",
        (ticket["id"],),
    )
    ticket["attachments"] = db_query(
        "SELECT filename, relative_path FROM ticket_attachments WHERE ticket_id = %s", (ticket["id"],)
    )
    return ticket


def search_tickets(query):
    return db_query(
        """SELECT ticket_number, client_raw, subject, status, priority, ticket_created
           FROM tickets
           WHERE subject ILIKE %s OR client_raw ILIKE %s OR description ILIKE %s OR ticket_number ILIKE %s
           ORDER BY ticket_created DESC NULLS LAST LIMIT 20""",
        tuple([f"%{query}%"] * 4),
    )


def get_invoice(identifier):
    order = db_query_one("SELECT * FROM sales_orders WHERE order_number = %s", (identifier.upper(),))
    if not order:
        order = db_query_one("SELECT * FROM sales_orders WHERE client_raw ILIKE %s LIMIT 1", (f"%{identifier}%",))
    if not order:
        return {"error": f"no sales order matching '{identifier}'"}
    order["lines"] = db_query(
        "SELECT product, description, qty, delivered, invoiced, unit_price, discount_pct, subtotal FROM order_lines WHERE sales_order_id = %s ORDER BY line_order",
        (order["id"],),
    )
    return order


def search_invoices(query):
    return db_query(
        """SELECT order_number, client_raw, order_date, amount_total, currency, state_label
           FROM sales_orders
           WHERE client_raw ILIKE %s OR order_number ILIKE %s
           ORDER BY order_date DESC NULLS LAST LIMIT 20""",
        (f"%{query}%", f"%{query}%"),
    )


def list_clients():
    return db_query("SELECT id, slug, display_name, domains, odoo_base_url FROM clients ORDER BY display_name")


def get_client_file(file_path):
    for table in ("tickets", "sales_orders", "call_transcripts", "wiki_pages"):
        row = db_query_one(f"SELECT * FROM {table} WHERE source_file_path = %s", (file_path,))
        if row:
            row.pop("body_tsv", None)
            row.pop("transcript_tsv", None)
            return row
    return {"error": f"no row found for file_path '{file_path}' in any loaded table"}


TOOLS = {
    "get_index": get_index,
    "get_wiki_page": get_wiki_page,
    "search_wiki": search_wiki,
    "list_emails": list_emails,
    "search_emails": search_emails,
    "get_email": get_email,
    "list_calls": list_calls,
    "search_calls": search_calls,
    "get_call": get_call,
    "get_ticket": get_ticket,
    "search_tickets": search_tickets,
    "get_invoice": get_invoice,
    "search_invoices": search_invoices,
    "list_clients": list_clients,
    "get_client_file": get_client_file,
}


@server.list_tools()
async def list_tools():
    return [
        Tool(
            name="get_index",
            description="Return row counts across all tables — the DB equivalent of the wiki index.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="get_wiki_page",
            description="Return a specific wiki page by title or partial match, with a body preview (first "
                        f"{BODY_PREVIEW_CHARS} chars, plus body_length/body_truncated), outbound links, and flags. "
                        "Full body is large — this intentionally previews rather than dumping the whole page.",
            inputSchema={"type": "object", "properties": {"title": {"type": "string"}}, "required": ["title"]},
        ),
        Tool(
            name="search_wiki",
            description="Full-text search synthesized wiki pages (entities, concepts, analyses, overviews, sources, prospects).",
            inputSchema={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
        ),
        Tool(
            name="list_emails",
            description="List email threads. account: 'all'|'raj_gmail'|'ron_gmail'|'remya_gmail'|'support_zoho'. month: 'YYYY-MM' or empty.",
            inputSchema={"type": "object", "properties": {
                "account": {"type": "string", "default": "all"}, "month": {"type": "string", "default": ""}}},
        ),
        Tool(
            name="search_emails",
            description="Full-text search raw email message bodies. account filter same as list_emails.",
            inputSchema={"type": "object", "properties": {
                "query": {"type": "string"}, "account": {"type": "string", "default": "all"}}, "required": ["query"]},
        ),
        Tool(
            name="get_email",
            description="Return a full email thread (all messages) by its source_file_path.",
            inputSchema={"type": "object", "properties": {"file_path": {"type": "string"}}, "required": ["file_path"]},
        ),
        Tool(
            name="list_calls",
            description="List call transcripts. source: 'fireflies'|'fathom' or omit for both. month: 'YYYY-MM' or empty.",
            inputSchema={"type": "object", "properties": {
                "month": {"type": "string", "default": ""}, "source": {"type": "string", "default": ""}}},
        ),
        Tool(
            name="search_calls",
            description="Full-text search call transcripts. source: 'fireflies'|'fathom' or omit for both — use this to answer e.g. 'latest Fathom calls'.",
            inputSchema={"type": "object", "properties": {
                "query": {"type": "string"}, "source": {"type": "string", "default": ""}}, "required": ["query"]},
        ),
        Tool(
            name="get_call",
            description="Return a full call transcript (all speaker segments) by its source_file_path.",
            inputSchema={"type": "object", "properties": {"file_path": {"type": "string"}}, "required": ["file_path"]},
        ),
        Tool(
            name="get_ticket",
            description="Return a support ticket by ticket number (e.g. 'T00123') or partial subject, with its full activity thread.",
            inputSchema={"type": "object", "properties": {"identifier": {"type": "string"}}, "required": ["identifier"]},
        ),
        Tool(
            name="search_tickets",
            description="Search support tickets by client, subject, description, or ticket number.",
            inputSchema={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
        ),
        Tool(
            name="get_invoice",
            description="Return a sales order/invoice by order number (e.g. 'S00123') or partial client name, with order lines.",
            inputSchema={"type": "object", "properties": {"identifier": {"type": "string"}}, "required": ["identifier"]},
        ),
        Tool(
            name="search_invoices",
            description="Search sales orders/invoices by client or order number.",
            inputSchema={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
        ),
        Tool(
            name="list_clients",
            description="List all clients in the registry (slug, display name, domains, Odoo instance).",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="get_client_file",
            description="Return a row from any loaded table by its original source_file_path (ticket, sales order, call, or wiki page).",
            inputSchema={"type": "object", "properties": {"file_path": {"type": "string"}}, "required": ["file_path"]},
        ),
    ]


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
