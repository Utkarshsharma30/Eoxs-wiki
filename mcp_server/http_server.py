"""SSE transport for mcp_server, exposing it as a remote MCP connector for
claude.ai. Reuses the exact same Server instance and tool definitions from
server.py (still used as-is for local/stdio access via Claude Code CLI or
Claude Desktop) -- only the transport differs.

Auth scheme matches the OLD vault system's already-working MCP connector
(raj-wiki-vault's mcp_server.py, confirmed live in the user's Claude
account) exactly, after an initial attempt at Streamable-HTTP +
Authorization-header auth turned out not to match what claude.ai's
"Add custom connector" dialog actually exposes (only an optional OAuth
Client Secret field -- no generic header input). The old server's own
comment explains the real pattern: "the server has no login, so the URL
path itself is the credential." No OAuth, no headers -- the SSE endpoint
is mounted under a long random secret path segment
(MCP_URL_SECRET in .env), so the URL itself is what gates access.

Run with: python -m mcp_server.http_server  (dev, binds 127.0.0.1 only)
Deployed via systemd as eoxs-mcp.service, reverse-proxied by nginx at
https://5.223.44.95/mcp/<MCP_URL_SECRET>/sse
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

from mcp_server.server import server as mcp_server_instance

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

MCP_URL_SECRET = os.environ["MCP_URL_SECRET"]

sse = SseServerTransport(f"/{MCP_URL_SECRET}/messages/")


async def _handle_sse_raw(scope, receive, send):
    async with sse.connect_sse(scope, receive, send) as streams:
        await mcp_server_instance.run(streams[0], streams[1], mcp_server_instance.create_initialization_options())
    return Response()


async def sse_endpoint(request):
    # Starlette's Route always wraps a plain function as func(request) -> response
    # (it doesn't special-case a 3-arg scope/receive/send signature) -- SSE needs
    # the raw ASGI call underneath, so unpack the Request back into that shape.
    return await _handle_sse_raw(request.scope, request.receive, request._send)


app = Starlette(
    routes=[
        Route(f"/{MCP_URL_SECRET}/sse", endpoint=sse_endpoint, methods=["GET"]),
        Mount(f"/{MCP_URL_SECRET}/messages/", app=sse.handle_post_message),
    ],
)


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("MCP_HTTP_PORT", "8091"))
    uvicorn.run(app, host="127.0.0.1", port=port)
