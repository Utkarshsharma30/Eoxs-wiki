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

Three identities, three secrets, three independent Server instances (see
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
  - MCP_GENERAL_URL_SECRET -> GENERAL_CLEARANCE (tier2 only). For any
    other employee's connector.

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

IDENTITIES = [
    ("full", os.environ["MCP_URL_SECRET"], FULL_CLEARANCE),
    ("hr", os.environ["MCP_HR_URL_SECRET"], HR_CLEARANCE),
    ("general", os.environ["MCP_GENERAL_URL_SECRET"], GENERAL_CLEARANCE),
]


def _make_routes(identity_name, secret, clearance):
    mcp_server_instance = build_server(clearance, name=f"eoxs-wiki-db-{identity_name}")
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


routes = [route for identity_name, secret, clearance in IDENTITIES for route in _make_routes(identity_name, secret, clearance)]

app = Starlette(routes=routes)


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("MCP_HTTP_PORT", "8091"))
    uvicorn.run(app, host="127.0.0.1", port=port)
