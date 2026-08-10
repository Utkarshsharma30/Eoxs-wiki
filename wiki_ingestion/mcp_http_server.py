"""Persistent HTTP/SSE MCP server for wiki-ingestion sub-agents, replacing
the previous per-invocation stdio spawn.

2026-08 root-cause fix, found by directly reproducing the failure live
(not inferred): `claude -p`'s connection to a freshly-spawned stdio MCP
subprocess was unreliable -- confirmed via --output-format stream-json's
system.init event showing `"mcp_servers":[{"name":"wiki","status":"pending"}]`
and NEVER updated again for the rest of the session, even though the
exact same server answers a raw MCP `initialize` handshake correctly in
well under a second when spawned directly (outside claude -p). Reproduced
deterministically and independent of environment (even under `env -i`
with everything but PATH/HOME stripped) -- so it's specific to claude -p's
own per-invocation stdio connection handling, not this server's code, not
resource contention, and not particular to any one session.

The external claude.ai connector (mcp_server/http_server.py) has never
shown this problem, because it's ONE persistent server being *reached*
over HTTP/SSE, not a brand-new subprocess being spawned and handshaken
fresh on every single call. This module applies the exact same fix: one
always-on HTTP/SSE server (systemd-managed, always running), reused by
every sub-agent invocation instead of being re-spawned per call.

Since this server now serves MANY invocations over its lifetime instead
of exactly one, cycle_id/source_kind (previously fixed per-process via
WIKI_CYCLE_ID/WIKI_SOURCE_KIND env vars, set once at subprocess spawn)
now come from the SSE connection's URL path instead -- a fresh, correctly
-scoped Server instance is built per connection via
agent_mcp_server.build_agent_server(cycle_id, source_kind), mirroring
mcp_server/http_server.py's per-identity build_server() pattern, just
resolved dynamically per request instead of for a small fixed set of
identities known in advance. Review and consolidation need no such
scoping (every tool takes explicit ids), so they get one shared
SseServerTransport/Server pair each.

Internal-only: binds 127.0.0.1, never proxied through nginx, no
secret-in-URL -- unlike the external claude.ai connector, there's no real
identity boundary to enforce here (only this box's own claude -p
processes ever connect), so cycle_id/source_kind in the URL are routing
keys, not credentials.

Three tool sets, three URL prefixes:
  /wiki-agent/{cycle_id}/{source_kind}/sse   -- Phase 3 (detect & draft)
  /wiki-review/sse                            -- Phase 5 (review)
  /wiki-consolidate/sse                       -- Phase 4 (consolidation)

Run with: python -m wiki_ingestion.mcp_http_server (dev, binds 127.0.0.1)
Deployed via systemd as eoxs-wiki-mcp.service (see deploy/).
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

from wiki_ingestion.agent_mcp_server import build_agent_server
from wiki_ingestion.review_mcp_server import build_review_server
from wiki_ingestion.consolidate_mcp_server import build_consolidate_server

load_dotenv(Path(__file__).resolve().parent.parent / ".env")


async def _run_session(mcp_server_instance, sse_transport, scope, receive, send):
    async with sse_transport.connect_sse(scope, receive, send) as streams:
        await mcp_server_instance.run(streams[0], streams[1], mcp_server_instance.create_initialization_options())
    return Response()


# --- Phase 3: draft, dynamically scoped per (cycle_id, source_kind) ---------

_agent_sse = SseServerTransport("/wiki-agent/messages/")


async def agent_sse_endpoint(request):
    cycle_id = int(request.path_params["cycle_id"])
    source_kind = request.path_params["source_kind"]
    srv = build_agent_server(cycle_id, source_kind)
    return await _run_session(srv, _agent_sse, request.scope, request.receive, request._send)


# --- Phase 5: review, no scoping needed --------------------------------------

_review_sse = SseServerTransport("/wiki-review/messages/")


async def review_sse_endpoint(request):
    srv = build_review_server()
    return await _run_session(srv, _review_sse, request.scope, request.receive, request._send)


# --- Phase 4: consolidation, no scoping needed -------------------------------

_consolidate_sse = SseServerTransport("/wiki-consolidate/messages/")


async def consolidate_sse_endpoint(request):
    srv = build_consolidate_server()
    return await _run_session(srv, _consolidate_sse, request.scope, request.receive, request._send)


routes = [
    Route("/wiki-agent/{cycle_id}/{source_kind}/sse", endpoint=agent_sse_endpoint, methods=["GET"]),
    Mount("/wiki-agent/messages/", app=_agent_sse.handle_post_message),
    Route("/wiki-review/sse", endpoint=review_sse_endpoint, methods=["GET"]),
    Mount("/wiki-review/messages/", app=_review_sse.handle_post_message),
    Route("/wiki-consolidate/sse", endpoint=consolidate_sse_endpoint, methods=["GET"]),
    Mount("/wiki-consolidate/messages/", app=_consolidate_sse.handle_post_message),
]

app = Starlette(routes=routes)


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("WIKI_MCP_HTTP_PORT", "8093"))
    uvicorn.run(app, host="127.0.0.1", port=port)
