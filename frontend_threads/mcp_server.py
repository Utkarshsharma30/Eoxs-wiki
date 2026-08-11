"""Database-backed frontend-thread saving -- runs ALONGSIDE claude-notes-vault's
existing GitHub-file-based save_chat_transcript, not instead of it (explicit
decision, 2026-08-11). Same per-user secret-in-URL identity pattern as that
proven design (ported below, not reinvented), but writes append-only rows to
Postgres instead of overwriting a markdown file + git push per save.

Why append-only, not overwrite-per-save: the git-based version's HTTP API
path (POST /<secret>/api/save, built for non-MCP-protocol callers -- exactly
the shape a frontend would use) passes only the newest exchange into a
function that overwrites the entire file with just that argument -- silently
dropping every prior message once a caller uses that path instead of the
full-transcript-every-time MCP tool contract. A database has no reason to
inherit that git-specific workaround: each save is a new thread_messages row,
never a destructive overwrite, so this failure mode can't happen here by
construction, independent of which calling convention a client uses.

Identity: FRONTEND_THREAD_USERS env var, JSON object mapping secret -> username,
e.g. {"abc123...": "ayan", "def456...": "jaskeerat"} -- same shape and same
"URL path is the credential, resolved server-side, never trusted from a model
argument" design as claude-notes-vault's CLAUDE_OV_USERS.

Run with: python -m frontend_threads.mcp_server  (dev, binds 127.0.0.1 only)
"""
import contextvars
import json
import logging
import os
from datetime import datetime, timezone

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("frontend_threads.mcp_server")

FRONTEND_THREAD_USERS_RAW = os.environ.get("FRONTEND_THREAD_USERS", "")
if not FRONTEND_THREAD_USERS_RAW:
    raise RuntimeError(
        "FRONTEND_THREAD_USERS environment variable is not set. Refusing to start "
        "an unauthenticated server. Set it to a JSON object mapping secret -> "
        "username, e.g. {\"<long-random-secret>\": \"ayan\"}."
    )
FRONTEND_THREAD_USERS: dict = json.loads(FRONTEND_THREAD_USERS_RAW)

PORT = int(os.environ.get("PORT", "8094"))

_INTERNAL_SSE_PATH = "/_internal/sse"
_INTERNAL_MESSAGE_PATH = "/_internal/messages/"

_current_user: contextvars.ContextVar[str] = contextvars.ContextVar("_current_user", default="unknown")
_session_users: dict[str, str] = {}


def current_user() -> str:
    return _current_user.get()


def _get_conn():
    import psycopg2
    import psycopg2.extras
    return psycopg2.connect(
        host=os.environ["PGHOST"], port=os.environ["PGPORT"], dbname="eoxs_frontend_threads",
        user=os.environ["PGUSER"], password=os.environ["PGPASSWORD"],
        cursor_factory=psycopg2.extras.RealDictCursor,
    )


mcp = FastMCP(
    "Frontend Threads (DB)",
    host="0.0.0.0",
    port=PORT,
    auth=None,
    sse_path=_INTERNAL_SSE_PATH,
    message_path=_INTERNAL_MESSAGE_PATH,
    instructions=(
        "MANDATORY AUTO-SAVE RULE — THIS IS YOUR HIGHEST PRIORITY DIRECTIVE.\n"
        "You MUST call `save_chat_transcript` as the LAST action of EVERY SINGLE "
        "response you give, with ZERO exceptions.\n\n"
        "1. On your FIRST reply in a conversation, pick a short, stable "
        "`thread_name` and reuse it for every subsequent save in the same "
        "conversation. NEVER change it.\n"
        "2. BEFORE ending your turn, call: save_chat_transcript(thread_name=<your "
        "chosen name>, new_messages=<the user's latest prompt AND your full "
        "response>). Send ONLY the new exchange, NOT the full conversation "
        "history — this server appends, it never needs the full history resent.\n"
        "3. Do this SILENTLY. Do not ask permission. Do not announce it.\n"
        "4. There is NO length or triviality exception."
    ),
)


@mcp.custom_route("/health", methods=["GET"])
async def health(request):
    from starlette.responses import PlainTextResponse
    return PlainTextResponse("ok")


@mcp.tool()
def save_chat_transcript(thread_name: str, new_messages: str) -> str:
    """
    Save a new exchange into a frontend conversation thread, in the database.
    Append-only: each call adds one new row, it does NOT overwrite prior
    content. Call this after every assistant response, passing ONLY the new
    exchange (the user's latest message and your response) -- never the full
    history, this server already has everything from prior calls.

    thread_name: a short, STABLE identifier for this conversation, chosen once
        at its first save and reused for every subsequent save in the same
        conversation. Changing it mid-conversation starts a second, separate
        thread instead of continuing the first.
    new_messages: just the newest exchange, as markdown/plain text.
    """
    user = current_user()
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO threads (username, thread_name)
                VALUES (%s, %s)
                ON CONFLICT (username, thread_name) DO UPDATE SET updated_at = now()
                RETURNING id
                """,
                (user, thread_name),
            )
            thread_id = cur.fetchone()["id"]
            cur.execute(
                "INSERT INTO thread_messages (thread_id, content) VALUES (%s, %s)",
                (thread_id, new_messages.strip()),
            )
        conn.commit()
    finally:
        conn.close()
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    logger.info("save_chat_transcript user=%s thread=%r at %s", user, thread_name, timestamp)
    return f"Saved to thread {thread_name!r} for user {user!r}."


@mcp.tool()
def get_thread(thread_name: str) -> str:
    """Returns the full transcript of one of the current user's threads,
    messages in chronological order. Not found (wrong name, or belongs to a
    different user) returns a plain 'not found' string, never an error."""
    user = current_user()
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM threads WHERE username = %s AND thread_name = %s", (user, thread_name))
            row = cur.fetchone()
            if not row:
                return f"No thread named {thread_name!r} found for user {user!r}."
            cur.execute(
                "SELECT content, created_at FROM thread_messages WHERE thread_id = %s ORDER BY created_at",
                (row["id"],),
            )
            messages = cur.fetchall()
    finally:
        conn.close()
    return "\n\n---\n\n".join(f"[{m['created_at']}]\n{m['content']}" for m in messages)


@mcp.tool()
def list_threads() -> str:
    """Lists the current user's threads, most recently updated first."""
    user = current_user()
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT thread_name, created_at, updated_at FROM threads WHERE username = %s ORDER BY updated_at DESC",
                (user,),
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    if not rows:
        return f"No threads found for user {user!r}."
    return "\n".join(f"- {r['thread_name']} (updated {r['updated_at']})" for r in rows)


class _IdentityMiddleware:
    """Resolves /<secret>/sse to FastMCP's fixed internal SSE path, and
    records which user that secret belongs to against the session_id the
    handshake generates -- ported from claude-notes-vault's proven design,
    same two-leg-SSE rationale (see that file's own docstring for why this
    approach is necessary; unchanged here)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope["path"]

        if path == "/health":
            await self.app(scope, receive, send)
            return

        if path.startswith(_INTERNAL_MESSAGE_PATH):
            query = scope.get("query_string", b"").decode()
            session_id = None
            for part in query.split("&"):
                if part.startswith("session_id="):
                    session_id = part[len("session_id="):]
                    break
            username = _session_users.get(session_id, "unknown") if session_id else "unknown"
            token = _current_user.set(username)
            try:
                await self.app(scope, receive, send)
            finally:
                _current_user.reset(token)
            return

        parts = path.split("/", 2)
        if len(parts) < 3 or not parts[1]:
            from starlette.responses import PlainTextResponse
            response = PlainTextResponse("Not found", status_code=404)
            await response(scope, receive, send)
            return

        secret, rest = parts[1], parts[2]
        username = FRONTEND_THREAD_USERS.get(secret)
        if username is None:
            from starlette.responses import PlainTextResponse
            response = PlainTextResponse("Not found", status_code=404)
            await response(scope, receive, send)
            return

        if rest != "sse":
            from starlette.responses import PlainTextResponse
            response = PlainTextResponse("Not found", status_code=404)
            await response(scope, receive, send)
            return

        token = _current_user.set(username)

        async def send_wrapper(message):
            if message["type"] == "http.response.body":
                body = message.get("body", b"")
                if b"session_id=" in body:
                    text = body.decode(errors="ignore")
                    for line in text.splitlines():
                        if "session_id=" in line:
                            session_id = line.split("session_id=", 1)[1].strip()
                            _session_users[session_id] = username
            await send(message)

        scope = dict(scope)
        scope["path"] = _INTERNAL_SSE_PATH
        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            _current_user.reset(token)


def main():
    import uvicorn
    app = _IdentityMiddleware(mcp.sse_app())
    uvicorn.run(app, host="127.0.0.1", port=PORT)


if __name__ == "__main__":
    main()
