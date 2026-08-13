"""SSE transport for mcp_server, exposing it as a remote MCP connector for
claude.ai. Reuses the exact same tool implementations from server.py (still
used as-is for local/stdio access via Claude Code CLI or Claude Desktop) --
only the transport differs, and here, unlike stdio, there IS a real
identity boundary to enforce.

Auth scheme matches the OLD vault system's already-working MCP connector
(raj-wiki-vault's mcp_server.py, confirmed live in the user's Claude
account) exactly, after an initial attempt at Streamable-HTTP +
Authorization-header auth turned out not to match what claude.ai's
"Add custom connector" dialog actually exposes (only an optional OAuth
Client Secret field -- no generic header input). The old server's own
comment explains the real pattern: "the server has no login, so the URL
path itself is the credential." No OAuth, no headers -- each identity's
SSE endpoint is mounted under its own long random secret path segment, so
the URL itself is what gates access AND which access_tier clearance the
connection gets.

Four identities, four secrets, four independent Server instances (see
server.py's build_server()) -- clearance is baked into each instance at
construction time, never derived from anything in the request, so there's
no header/param a client could send to widen its own access:

  - MCP_URL_SECRET         -> FULL_CLEARANCE (tier1 + tier2_confidential +
    tier2). This is the secret already live in claude.ai before tiering
    existed -- left pointing at full access so whoever already has it
    (presumably Raj) keeps working unchanged. Confirm who currently holds
    this URL before handing it out further.
  - MCP_HR_URL_SECRET      -> HR_CLEARANCE (tier2_confidential + tier2,
    not tier1/Raj-personal). For HR and other explicitly-trusted roles.
  - MCP_GENERAL_URL_SECRET -> HR_CLEARANCE (tier2_confidential + tier2,
    not tier1/Raj-personal), plus extra_redact_categories=
    ("monetary_amounts", "employee_activity_monitoring"): every dollar
    figure gets stripped (including payroll -- unlike HR, no carve-out)
    and Cattr/performance-monitoring content stays HR+full-only regardless
    of the wider tier clearance. 2026-08-11: widened from tier2-only,
    since most tier2_confidential pages carry a dollar figure alongside
    otherwise-relevant general content that general shouldn't lose over
    one number -- see redaction.py for the category definitions.
  - MCP_INTERN_URL_SECRET  -> GENERAL_CLEARANCE (tier2 only -- unlike
    general above, intern was NOT widened to tier2_confidential), plus
    extra_redact_categories=("monetary_amounts",): every tool response
    also gets checked for dollar figures/prices/totals/deal sizes and has
    them stripped, on top of the normal tier2-only filtering. For interns
    -- same data as any other employee, minus every number that's money.

2026-08-12: `full` and `hr` also get the employees.py tool set (the first
write-capable tools this server has ever exposed) -- list/search/get plus
create/update/deactivate/reactivate_employee, gated independently of
`clearance` via `enable_employee_tools` (general/intern get none of it,
even though `general` otherwise shares HR_CLEARANCE with `hr`). See
mcp_server/employees.py and schema/025_employees.sql.

2026-08-13: `full` and `hr` also get mcp_server/asset_writes.py's
create_asset/update_asset -- the second and, per explicit instruction, last
write surface in this server. `full` is unrestricted; `hr` gets
update_asset ONLY, and only for `SALARY_ASSET_SLUG` ('eoxs-salary-details')
-- any other slug is refused with a plain permission error. general/intern
get neither tool. See schema/032_asset_change_log.sql.

2026-08-13: a 5th identity, `staging_qa`, exists purely for QA-testing
write behavior (does the model create/update the right rows, does it ever
touch something it shouldn't) with zero risk to live data. Full clearance,
every read tool, both employee and asset write tools fully unrestricted --
but every single tool call this identity makes, read or write, is
transparently routed to the eoxs_wiki_staging DATABASE instead of live
eoxs_wiki (see mcp_server/db.py's use_database()/ContextVar and
server.py's build_server() `database` param). This is NOT the `wiki_staging`
SCHEMA that live drafts sit in before promotion (schema/017's comment) --
it's the other, unrelated "staging" concept: a full separate physical
database eoxs_wiki_staging, which wiki_ingestion has no code path to reach
at all (confirmed by grep: zero references to get_staging_conn/
PGDATABASE_STAGING anywhere in wiki_ingestion/). A write through this
identity can therefore never reach the wiki-ingestion pipeline, structurally,
not just by policy. Every write is also tagged `changed_by='staging_qa'` in
employee_change_log/asset_change_log, so QA activity is trivially
distinguishable from anything else. See loaders/reset_staging_qa_data.py to
wipe staging's employees/assets tables and re-mirror them from live for a
clean slate between QA sessions.

Run with: python -m mcp_server.http_server  (dev, binds 127.0.0.1 only)
Deployed via systemd as eoxs-mcp.service, reverse-proxied by nginx at
https://5.223.44.95/mcp/<secret>/sse -- nginx passes /mcp/ through
unchanged (no prefix stripping) for both secrets alike, see
deploy/nginx-https.conf's single generic `location /mcp/` block.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
from starlette.applications import Starlette
from starlette.routing import Mount, Route
from starlette.responses import Response

from mcp.server.sse import SseServerTransport

from mcp_server.server import build_server, FULL_CLEARANCE, HR_CLEARANCE, GENERAL_CLEARANCE

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# Mounted with the /mcp prefix baked in (matching nginx's location /mcp/,
# which passes the URI through UNCHANGED rather than stripping it -- see
# deploy/nginx-https.conf). SseServerTransport uses this path to build the
# "endpoint" event it sends the client (telling it where to POST messages),
# so the app must know its real external path, not just its local one --
# otherwise that URL is missing /mcp, the client's POST 404s against
# nginx's default location (the OTHER app on port 8090), and the session
# hangs after a successful-looking initial SSE connection.
MOUNT_PREFIX = "/mcp"

SALARY_ASSET_SLUG = "eoxs-salary-details"

IDENTITIES = [
    # 5th element: enable_employee_tools (see server.py's build_server()
    # docstring) -- general/intern get NO employee tools at all, even
    # though 'general' otherwise shares HR_CLEARANCE's clearance with 'hr'.
    # 6th element: asset_write_scope -- "all" (full, unrestricted),
    # a specific slug set (hr, salary register only), or None (general/
    # intern, no asset write tools at all). Independent of `clearance` for
    # the same reason enable_employee_tools is: these are the only two
    # write surfaces in the whole server, deliberately gated identity-by-
    # identity, never by the read-side tier system.
    # 7th element: database -- None (live, every real identity) or
    # "staging" (staging_qa only -- routes every tool call to
    # eoxs_wiki_staging, see mcp_server/db.py's use_database()).
    ("full", os.environ["MCP_URL_SECRET"], FULL_CLEARANCE, (), True, "all", None),
    ("hr", os.environ["MCP_HR_URL_SECRET"], HR_CLEARANCE, ("non_payroll_monetary_amounts",), True, {SALARY_ASSET_SLUG}, None),
    # 2026-08-11: expanded from GENERAL_CLEARANCE (tier2 only) to HR_CLEARANCE
    # (tier2_confidential + tier2) -- most tier2_confidential pages carry a
    # dollar figure alongside otherwise-relevant general content, and general
    # was losing the whole page over one number. Same URL/secret as before,
    # so nothing breaks for anyone who already has this link -- it now just
    # returns more, redacted content. monetary_amounts blocks every dollar
    # figure (including payroll -- unlike hr's non_payroll_monetary_amounts
    # carve-out); employee_activity_monitoring keeps Cattr/performance data
    # HR+full-only regardless of the wider tier clearance (see redaction.py).
    ("general", os.environ["MCP_GENERAL_URL_SECRET"], HR_CLEARANCE, ("monetary_amounts", "employee_activity_monitoring"), False, None, None),
    ("intern", os.environ["MCP_INTERN_URL_SECRET"], GENERAL_CLEARANCE, ("monetary_amounts",), False, None, None),
    # staging_qa: full clearance, every read tool, BOTH write tool sets
    # fully unrestricted (unlike hr's real-world restrictions -- there's
    # nothing to protect here, it's disposable test data) -- but database=
    # "staging" means every single call, read or write, hits
    # eoxs_wiki_staging, never live. Deliberately not documented in any of
    # the 4 main skill files -- this identity is for direct hands-on QA by
    # whoever holds the secret, not for a persistent claude.ai connector
    # meant to answer real questions. See deploy/eoxs-wiki-db-skill-staging-qa.md.
    ("staging_qa", os.environ["MCP_STAGING_URL_SECRET"], FULL_CLEARANCE, (), True, "all", "staging"),
]


def _make_routes(identity_name, secret, clearance, extra_redact_categories=(), enable_employee_tools=False, asset_write_scope=None, database=None):
    mcp_server_instance = build_server(
        clearance, name=f"eoxs-wiki-db-{identity_name}", extra_redact_categories=extra_redact_categories,
        enable_employee_tools=enable_employee_tools, identity_name=identity_name, asset_write_scope=asset_write_scope,
        database=database,
    )
    sse = SseServerTransport(f"{MOUNT_PREFIX}/{secret}/messages/")

    async def _handle_sse_raw(scope, receive, send):
        async with sse.connect_sse(scope, receive, send) as streams:
            await mcp_server_instance.run(streams[0], streams[1], mcp_server_instance.create_initialization_options())
        return Response()

    async def sse_endpoint(request):
        # Starlette's Route always wraps a plain function as func(request) -> response
        # (it doesn't special-case a 3-arg scope/receive/send signature) -- SSE needs
        # the raw ASGI call underneath, so unpack the Request back into that shape.
        return await _handle_sse_raw(request.scope, request.receive, request._send)

    return [
        Route(f"{MOUNT_PREFIX}/{secret}/sse", endpoint=sse_endpoint, methods=["GET"]),
        Mount(f"{MOUNT_PREFIX}/{secret}/messages/", app=sse.handle_post_message),
    ]


routes = [
    route
    for identity_name, secret, clearance, extra, enable_employee_tools, asset_write_scope, database in IDENTITIES
    for route in _make_routes(identity_name, secret, clearance, extra, enable_employee_tools, asset_write_scope, database)
]

app = Starlette(routes=routes)


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("MCP_HTTP_PORT", "8091"))
    uvicorn.run(app, host="127.0.0.1", port=port)
