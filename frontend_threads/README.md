# Frontend Threads (DB-backed)

Database-backed `save_chat_transcript`, running **alongside** (not instead
of) `claude-notes-vault`'s existing GitHub-file-based version — same
per-user secret-in-URL identity pattern, but append-only Postgres rows
instead of overwrite-and-git-push per save. See `mcp_server.py`'s module
docstring for the full design rationale, including the specific failure
mode in the git-based version this was built to avoid by construction.

## One-time setup

1. **Create the database** (needs a Postgres superuser — run this yourself):
   ```
   sudo -u postgres psql -c "CREATE DATABASE eoxs_frontend_threads;"
   ```
2. **Apply the schema**:
   ```
   cd /home/deploy/eoxs-wiki-db
   .venv/bin/python3 -m frontend_threads.schema.apply
   ```
3. **Register users** — add to `.env`:
   ```
   FRONTEND_THREAD_USERS={"<long-random-secret-per-user>": "<username>"}
   ```
   Generate a secret per user with `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`.
   Each user's connector URL is `https://<host>/<their-secret>/sse`.
4. **Start the service**:
   ```
   sudo cp deploy/eoxs-frontend-threads.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now eoxs-frontend-threads.service
   ```
5. **Route it in nginx** — add a location block (mirroring the existing
   `/mcp/` block) proxying to `127.0.0.1:8094` (or whatever `PORT` is set
   to), under whichever public path you want to expose per-user secrets on.

## Tools

- `save_chat_transcript(thread_name, new_messages)` — append one new
  exchange. Never send full history; this server already has everything
  from prior calls for the same thread.
- `get_thread(thread_name)` — full transcript, chronological.
- `list_threads()` — the calling user's threads, most recently updated first.

## What's deliberately not built (yet)

Search across threads, cross-user access, and anything beyond the save/read
basics — out of scope for what was asked. Add if actually needed.
