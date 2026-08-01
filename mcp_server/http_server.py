"""HTTP transport for mcp_server, exposing it as a remote MCP connector
(claude.ai custom connectors, or any Streamable-HTTP MCP client) over the
existing nginx+TLS setup. Reuses the exact same Server instance and tool
definitions from server.py (still used as-is for local/stdio access via
Claude Code CLI or Claude Desktop) -- only the transport differs.

Auth: a single shared bearer token (MCP_HTTP_TOKEN in .env), checked by
a plain ASGI middleware against the raw request headers before the MCP
session manager ever sees the request -- deliberately not
starlette.middleware.base.BaseHTTPMiddleware, which buffers responses in
a way that doesn't play well with Streamable HTTP's SSE streaming.
claude.ai's custom connector "Request headers" field sends
Authorization: Bearer <token> on every request, which is exactly this
scheme -- no OAuth authorization server needed (confirmed via Claude's
own docs: fixed-credential servers are supported directly).

Run with: python -m mcp_server.http_server  (dev, binds 127.0.0.1 only)
Deployed via systemd as eoxs-mcp.service, reverse-proxied by nginx at
https://5.223.44.95/mcp/
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import JSONResponse
from starlette.routing import Mount

from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

from mcp_server.server import server as mcp_server_instance

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

MCP_HTTP_TOKEN = os.environ["MCP_HTTP_TOKEN"]

session_manager = StreamableHTTPSessionManager(app=mcp_server_instance)


class BearerAuthMiddleware:
    """Plain ASGI middleware -- checks the Authorization header directly
    off the raw scope, never touching the request body stream, so it's
    safe for Streamable HTTP's long-lived SSE responses."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        auth = headers.get(b"authorization", b"").decode("utf-8", errors="replace")
        if auth != f"Bearer {MCP_HTTP_TOKEN}":
            response = JSONResponse({"error": "unauthorized"}, status_code=401)
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


class MCPASGIApp:
    async def __call__(self, scope, receive, send):
        await session_manager.handle_request(scope, receive, send)


app = Starlette(
    routes=[Mount("/", app=MCPASGIApp())],
    middleware=[Middleware(BearerAuthMiddleware)],
    lifespan=lambda app: session_manager.run(),
)


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("MCP_HTTP_PORT", "8091"))
    uvicorn.run(app, host="127.0.0.1", port=port)
