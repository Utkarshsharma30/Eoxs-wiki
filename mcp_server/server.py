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
            (SELECT count(*) FROM clients) AS clients,
            (SELECT count(*) FROM implementation_tasks) AS implementation_tasks
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


def get_email(identifier):
    """identifier: the row's numeric id (from a search/list result -- REQUIRED for
    API-ingested threads, which always have a NULL source_file_path) or a legacy
    source_file_path string (only ever set for the historical file-based load)."""
    if str(identifier).isdigit():
        thread = db_query_one("SELECT * FROM email_threads WHERE id = %s", (int(identifier),))
    else:
        thread = db_query_one("SELECT * FROM email_threads WHERE source_file_path = %s", (identifier,))
    if not thread:
        return {"error": f"no email thread matching '{identifier}'"}
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


def get_call(identifier):
    """identifier: the row's numeric id (from a search/list result -- REQUIRED for
    API-ingested calls, i.e. every Fireflies/Fathom call, which always have a NULL
    source_file_path) or a legacy source_file_path string (only ever set for the
    historical file-based load)."""
    if str(identifier).isdigit():
        call = db_query_one("SELECT * FROM call_transcripts WHERE id = %s", (int(identifier),))
    else:
        call = db_query_one("SELECT * FROM call_transcripts WHERE source_file_path = %s", (identifier,))
    if not call:
        return {"error": f"no call matching '{identifier}'"}
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


def list_contacts(client=""):
    sql = """
        SELECT c.name, c.email, c.is_relay_inbox, cl.slug AS client
        FROM contacts c JOIN clients cl ON cl.id = c.client_id
        WHERE 1=1
    """
    params = []
    if client:
        sql += " AND cl.slug = %s"
        params.append(client)
    sql += " ORDER BY cl.display_name, c.name"
    return db_query(sql, params)


def get_client_profile(client):
    """Aggregates everything linked to one client by client_id in a single
    call -- every raw table (tickets, email_threads, call_transcripts,
    implementation_tasks, sales_orders) has a client_id column, but every
    other tool here only searches ONE of them at a time, so answering
    something like "give me everything on client X" required chaining 5+
    separate tool calls with no guarantee the caller actually would. This
    is the single-call alternative: client record, contacts, and recent-
    activity summaries across every source, cross-linked by client_id --
    not exhaustive detail (use get_ticket/get_email/get_call/
    get_implementation_task for that), but enough to see the whole
    relationship at a glance and know what to drill into.

    NOTE on scope: only searches LIVE tables. Synthesized wiki content for
    this client may exist in wiki_staging (reviewed, not yet promoted) --
    that's flagged by staging_wiki_pages_pending_promotion below (a count
    and titles only, not body content -- staging is not part of this
    read-only server's live-data contract) so the caller knows to ask a
    human to check staging or promote first, rather than concluding no
    wiki content exists.
    """
    row = db_query_one("SELECT id, slug, display_name, domains, odoo_base_url, odoo_db FROM clients WHERE slug = %s", (client,))
    if not row:
        row = db_query_one("SELECT id, slug, display_name, domains, odoo_base_url, odoo_db FROM clients WHERE display_name ILIKE %s", (f"%{client}%",))
    if not row:
        return {"error": f"no client matching '{client}' -- see list_clients for valid slugs"}
    client_id = row["id"]

    row["contacts"] = db_query(
        "SELECT name, email, is_relay_inbox FROM contacts WHERE client_id = %s ORDER BY name", (client_id,)
    )
    row["tickets"] = db_query(
        """SELECT ticket_number, subject, status, priority, ticket_created
           FROM tickets WHERE client_id = %s ORDER BY ticket_created DESC NULLS LAST LIMIT 20""",
        (client_id,),
    )
    row["ticket_count"] = db_query_one("SELECT count(*) AS n FROM tickets WHERE client_id = %s", (client_id,))["n"]
    row["implementation_tasks_recent"] = db_query(
        """SELECT task_name, stage, owner, task_created_date
           FROM implementation_tasks WHERE client_id = %s ORDER BY task_created_date DESC NULLS LAST LIMIT 20""",
        (client_id,),
    )
    row["implementation_task_count"] = db_query_one(
        "SELECT count(*) AS n FROM implementation_tasks WHERE client_id = %s", (client_id,)
    )["n"]
    row["emails_recent"] = db_query(
        """SELECT id, source_account, subject FROM email_threads
           WHERE client_id = %s ORDER BY id DESC LIMIT 20""",
        (client_id,),
    )
    row["email_count"] = db_query_one("SELECT count(*) AS n FROM email_threads WHERE client_id = %s", (client_id,))["n"]
    row["calls_recent"] = db_query(
        """SELECT id, source, meeting_title, call_date FROM call_transcripts
           WHERE client_id = %s ORDER BY call_date DESC NULLS LAST LIMIT 20""",
        (client_id,),
    )
    row["call_count"] = db_query_one("SELECT count(*) AS n FROM call_transcripts WHERE client_id = %s", (client_id,))["n"]
    row["sales_orders_count"] = db_query_one("SELECT count(*) AS n FROM sales_orders WHERE client_id = %s", (client_id,))["n"]

    row["live_wiki_pages"] = db_query(
        "SELECT title, page_type FROM wiki_pages WHERE title ILIKE %s ORDER BY title", (f"%{row['display_name']}%",)
    )
    staging_pending = db_query(
        """SELECT title, page_type, status FROM wiki_staging.wiki_pages
           WHERE title ILIKE %s AND status = 'reviewed' ORDER BY title""",
        (f"%{row['display_name']}%",),
    )
    row["staging_wiki_pages_pending_promotion"] = {
        "count": len(staging_pending),
        "titles": [p["title"] for p in staging_pending],
        "note": "Reviewed but not yet promoted to live wiki_pages -- ask a human to promote, or query wiki_staging directly for full content.",
    }
    return row


def get_client_file(file_path):
    for table in ("tickets", "sales_orders", "call_transcripts", "wiki_pages"):
        row = db_query_one(f"SELECT * FROM {table} WHERE source_file_path = %s", (file_path,))
        if row:
            row.pop("body_tsv", None)
            row.pop("transcript_tsv", None)
            return row
    return {"error": f"no row found for file_path '{file_path}' in any loaded table"}


def list_implementation_tasks(client="", stage=""):
    sql = """
        SELECT it.id, c.slug AS client, it.project_name, it.task_name, it.stage, it.owner,
               it.priority, it.kanban_state, it.active, it.task_created_date, it.deadline
        FROM implementation_tasks it JOIN clients c ON c.id = it.client_id
        WHERE 1=1
    """
    params = []
    if client:
        sql += " AND c.slug = %s"
        params.append(client)
    if stage:
        sql += " AND it.stage = %s"
        params.append(stage)
    sql += " ORDER BY it.task_created_date DESC NULLS LAST LIMIT 100"
    return db_query(sql, params)


def search_implementation_tasks(query, client=""):
    sql = """
        SELECT it.id, c.slug AS client, it.task_name, it.stage, it.owner, it.priority,
               it.task_created_date
        FROM implementation_tasks it JOIN clients c ON c.id = it.client_id
        WHERE (it.task_name ILIKE %s OR it.description ILIKE %s)
    """
    params = [f"%{query}%", f"%{query}%"]
    if client:
        sql += " AND c.slug = %s"
        params.append(client)
    sql += " ORDER BY it.task_created_date DESC NULLS LAST LIMIT 20"
    return db_query(sql, params)


def get_implementation_task(task_id):
    """Looks up by the serial `id` first, falling back to `odoo_task_id`.
    The fallback exists because odoo_fetcher.py full-refreshes (DELETE+
    INSERT) this table on every raw-ingestion sweep, so `id` isn't stable
    across time the way `odoo_task_id` is -- a caller (like the
    wiki-ingestion pipeline, which can reference a task hours or days
    after first seeing it) needs a lookup that still resolves after `id`
    has shifted. The two id spaces don't overlap (id: 15000s+, odoo_task_id:
    under 1000, verified empirically), so trying `id` first is unambiguous
    and doesn't change behavior for any existing caller passing a real id."""
    task = db_query_one(
        """SELECT it.*, c.slug AS client_slug, c.display_name AS client_name
           FROM implementation_tasks it JOIN clients c ON c.id = it.client_id
           WHERE it.id = %s""",
        (task_id,),
    )
    if not task:
        task = db_query_one(
            """SELECT it.*, c.slug AS client_slug, c.display_name AS client_name
               FROM implementation_tasks it JOIN clients c ON c.id = it.client_id
               WHERE it.odoo_task_id = %s""",
            (task_id,),
        )
    if not task:
        return {"error": f"no implementation task with id or odoo_task_id={task_id}"}
    task["events"] = db_query(
        """SELECT event_order, event_type, author, event_time, body, tracking_changes
           FROM implementation_task_events WHERE task_id = %s ORDER BY event_order""",
        (task["id"],),
    )
    task["attachments"] = db_query(
        "SELECT filename, mimetype, size_bytes FROM implementation_task_attachments WHERE task_id = %s",
        (task["id"],),
    )
    return task


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
    "list_contacts": list_contacts,
    "get_client_profile": get_client_profile,
    "get_client_file": get_client_file,
    "list_implementation_tasks": list_implementation_tasks,
    "search_implementation_tasks": search_implementation_tasks,
    "get_implementation_task": get_implementation_task,
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
            description="Return a full email thread (all messages) by the 'id' from a list_emails/search_emails "
                        "result. Also accepts a legacy source_file_path string, but every live-ingested (Gmail/"
                        "Zoho) thread has a NULL source_file_path -- use 'id' for those, which is always present.",
            inputSchema={"type": "object", "properties": {"identifier": {"type": "string"}}, "required": ["identifier"]},
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
            description="Return a full call transcript (all speaker segments) by the 'id' from a list_calls/"
                        "search_calls result. Also accepts a legacy source_file_path string, but every "
                        "live-ingested (Fireflies/Fathom) call has a NULL source_file_path -- use 'id' for "
                        "those, which is always present.",
            inputSchema={"type": "object", "properties": {"identifier": {"type": "string"}}, "required": ["identifier"]},
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
            name="list_contacts",
            description="List known contacts (name, email) for a client, or all clients if omitted. "
                        "client: client slug (e.g. 'sabre-alloys') or empty for all.",
            inputSchema={"type": "object", "properties": {"client": {"type": "string", "default": ""}}},
        ),
        Tool(
            name="get_client_profile",
            description="THE tool for 'tell me everything about client X' -- aggregates the client record, "
                        "contacts, recent tickets, recent implementation tasks, recent emails, recent calls, "
                        "sales order count, and live+pending-promotion wiki pages, all cross-linked by client_id "
                        "in one call. Prefer this over chaining separate search_* calls for a client overview; "
                        "use the individual get_ticket/get_email/get_call/get_implementation_task tools to drill "
                        "into any one item this surfaces. client: slug (e.g. 'sabre-alloys') or a display-name substring.",
            inputSchema={"type": "object", "properties": {"client": {"type": "string"}}, "required": ["client"]},
        ),
        Tool(
            name="get_client_file",
            description="Return a row from any loaded table by its original source_file_path (ticket, sales order, call, or wiki page).",
            inputSchema={"type": "object", "properties": {"file_path": {"type": "string"}}, "required": ["file_path"]},
        ),
        Tool(
            name="list_implementation_tasks",
            description="List Odoo implementation Kanban tasks (client onboarding/dev tasks -- distinct from support tickets). "
                        "client: client slug (e.g. 'greer-steel') or empty for all. stage: exact stage name (e.g. 'Completed') or empty for all.",
            inputSchema={"type": "object", "properties": {
                "client": {"type": "string", "default": ""}, "stage": {"type": "string", "default": ""}}},
        ),
        Tool(
            name="search_implementation_tasks",
            description="Full-text search Odoo implementation Kanban tasks by task name or description. "
                        "client: client slug to restrict to one client, or empty for all.",
            inputSchema={"type": "object", "properties": {
                "query": {"type": "string"}, "client": {"type": "string", "default": ""}}, "required": ["query"]},
        ),
        Tool(
            name="get_implementation_task",
            description="Return a full implementation task (description, stage/owner/priority, all chatter events "
                        "including stage-change history, and attachment metadata) by its numeric id from a "
                        "list/search_implementation_tasks result.",
            inputSchema={"type": "object", "properties": {"task_id": {"type": "integer"}}, "required": ["task_id"]},
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
