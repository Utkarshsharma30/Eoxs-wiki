# Backend Server

*Deep technical reference for anyone being onboarded to backend-server work on Cruz (`eoxs-wiki-db`).*

## 1. Where this runs

One VPS: hostname `ubuntu-4gb-sin-1`, reachable at bare IP `5.223.44.95` — **no domain name**. Repo lives at `/home/deploy/eoxs-wiki-db`. Every application service runs as Linux user `deploy` (uid 1000). Note: `deploy` is also in the `sudo` group — if any of these services were ever compromised, the attacker would inherit sudo-capable access, not a locked-down service account. Tightening this (a dedicated non-sudo service user) is a reasonable hardening task for a future developer, not something currently done.

## 2. What's running here, service by service

All app-level systemd units live in `deploy/` in the repo (installed as `/etc/systemd/system/*`). Every unit sets `User=deploy`, `Group=deploy`, `WorkingDirectory=/home/deploy/eoxs-wiki-db`, `EnvironmentFile=/home/deploy/eoxs-wiki-db/.env`, and logs to journald (`StandardOutput=journal`, distinct `SyslogIdentifier` per unit — there are no separate log files; `journalctl -u <unit>` is the only place to look).

| Unit | Type | What it runs | Cadence |
|---|---|---|---|
| `eoxs-ingestion.service` | simple, `Restart=always` | `python3 -m ingestion.server` | always-on |
| `eoxs-mcp.service` | simple, `Restart=always` | `python3 -m mcp_server.http_server` | always-on |
| `eoxs-wiki-mcp.service` | simple, `Restart=always` | `python3 -m wiki_ingestion.mcp_http_server` | always-on — internal-only MCP server the wiki-pipeline's `claude -p` sub-agents connect to, never customer-facing |
| `eoxs-sweep.service` | oneshot | `python3 -m ingestion.server --sweep` | triggered by its timer |
| `eoxs-sweep.timer` | timer | triggers the above | `OnCalendar=00/2:00:00`, `RandomizedDelaySec=120`, `Persistent=true` — every 2 hours, ±2min jitter, catches up after a reboot |
| `eoxs-wiki-pipeline.service` | oneshot, `TimeoutStartSec=infinity` | `python3 -m wiki_ingestion.run_pipeline` | triggered by its timer |
| `eoxs-wiki-pipeline.timer` | timer | triggers the above | `OnCalendar=00/6:00:00`, `RandomizedDelaySec=120`, `Persistent=true` — every 6 hours |
| `pgweb.service` | simple, `Restart=always` | `pgweb --bind=127.0.0.1 --listen=8092 --url=postgres://eoxs_readonly@localhost:5432/eoxs_wiki --readonly --lock-session --auth-user=dbadmin ...` | always-on |
| `nginx.service` | system-provided | reverse proxy (see §4) | always-on |

A sibling system, `eoxssecondbrain/eoxs-frontend-threads` (a separate repo, its own clone at `/home/deploy/eoxs-frontend-threads`, its own venv, its own `eoxs-frontend-threads.service`), runs on this same physical box but is **not** part of this repo's codebase — deliberately split out so raw frontend-chat-thread volume never bloats this system, same reasoning as `claude-notes-vault`'s split from `raj-wiki-vault`. It has its own dedicated `eoxs_frontend_threads` database on this same Postgres instance.

`eoxs-wiki-pipeline.service`'s `TimeoutStartSec=infinity` is deliberate — it runs sequential `claude -p` sub-agent calls that can take 3–5+ minutes per batch and hours for a full run, which would otherwise hit systemd's default ~90s oneshot timeout and get killed mid-run.

**Known secret-hygiene issue**: the `pgweb.service` unit interpolates `${PGWEB_DB_PASSWORD}` and `${PGWEB_HTTP_PASSWORD}` directly into its `ExecStart=` command line. That means both secrets are visible in plaintext to anyone able to run `systemctl status pgweb` or `ps` as *any* local user, not just root. Worth fixing (e.g. wrap the actual pgweb invocation in a small shell script that reads the env vars internally instead of interpolating them into the unit file) — flagged here rather than silently left for the next person to discover the hard way.

## 3. The Ingestion Server (`ingestion/server.py`, 261 lines)

FastAPI app, `title="eoxs-wiki-db Raw Ingestion Server"`. Port: `INGESTION_SERVER_PORT` env var (default **8090**), binds `0.0.0.0` — reachable only via nginx's reverse proxy in practice (see §4), not directly from the internet.

Routes:

| Route | Purpose | Auth |
|---|---|---|
| `GET /health` | `{status, run_in_progress, last_run}` | none |
| `POST /webhook/gmail` | Gmail Pub/Sub push (`{emailAddress, historyId}` base64 envelope) — refetches **all 3** Gmail accounts in the background, always returns 200 immediately | none (URL itself is the secret) |
| `POST /webhook/fireflies` | Fireflies `transcript.completed` webhook | HMAC-SHA256 via `FIREFLIES_WEBHOOK_SECRET`, checked only if the var is set |
| `POST /webhook/fathom` | Fathom `recording.completed` webhook | Svix-style HMAC via `FATHOM_WEBHOOK_SECRET`, checked only if the var is set |
| `POST /trigger/manual` | Manual full sweep across all 7 sources; the only trigger path that reports to Linear | Bearer token against `INGESTION_WEBHOOK_SECRET`, if set |

A module-level `asyncio.Lock` (`_sweep_lock`) ensures only one run executes at a time — a duplicate trigger while one is in-flight is logged and dropped, not queued.

Zoho and Odoo (tickets, invoices, all per-client implementation boards) have **no webhook path at all** — they're only ever picked up by the 2-hourly sweep. See `docs/raw-ingestion.md` for the full per-source breakdown.

The actual production sweep entrypoint is `python -m ingestion.server --sweep` (the argparse branch, run directly by `eoxs-sweep.timer`) — not the HTTP `/trigger/manual` route.

## 4. Reverse Proxy & TLS (nginx)

HTTPS on the bare server IP `5.223.44.95` — no domain anywhere in the config. Live config: `/etc/nginx/sites-enabled/eoxs-ingestion`, tracked in the repo at `deploy/nginx-https.conf` (confirmed byte-identical to what's actually deployed).

Two-phase design, both phases kept in `deploy/`:
- `deploy/nginx-http-only.conf` — the bootstrap config used before a certificate existed (port 80 only, serves the ACME HTTP-01 challenge, proxies everything else to 8090). No longer live.
- `deploy/nginx-https.conf` — the current live config:
  - **Port 80**: serves `/.well-known/acme-challenge/` from `/var/www/certbot` (kept alive for renewal — IP-address certs are short-lived, roughly a 6–7 day renewal cycle via HTTP-01), 301-redirects everything else to HTTPS.
  - **Port 443** (`ssl default_server`): cert/key at `/etc/letsencrypt/live/5.223.44.95/fullchain.pem` / `privkey.pem`. Three location blocks:
    - `location /mcp/` → `proxy_pass http://127.0.0.1:8091` (no trailing slash, deliberately — the SSE app bakes `/mcp` into its own self-referential URLs). `proxy_buffering off`, `proxy_read_timeout 3600s`, HTTP/1.1 — all SSE-streaming-specific settings.
    - `location /dbadmin/` → `proxy_pass http://127.0.0.1:8092/` (pgweb, trailing slash strips the prefix). Protected by nginx `auth_basic` against `/etc/nginx/.htpasswd_dbadmin` — a third independent layer on top of pgweb's own `--readonly`/`--lock-session` flags and the `eoxs_readonly` Postgres role's SELECT-only grants.
    - `location /` → `proxy_pass http://127.0.0.1:8090` (the ingestion server, catch-all).

`deploy/reload-nginx.sh` is installed as a certbot deploy-hook (`/etc/letsencrypt/renewal-hooks/deploy/`) — it runs `systemctl reload nginx` automatically after every successful renewal so nginx always serves the current cert. Automatic renewal itself is confirmed live via `snap.certbot.renew.timer`.

The ingestion server's logs show routine internet-scanner noise (`GET /mysql-admin/`, `/cgi-bin/snapshot.cgi`, etc., all 404) — expected background noise for any bare-IP HTTPS listener with no WAF, not a sign of compromise.

## 5. MCP Server (`mcp_server/`)

This is where the access-rights system is actually enforced. See `docs/postgres-database.md` for the `access_tier` column itself; this section is about the serving layer.

- `mcp_server/db.py` — thin psycopg2 helper (`get_conn`, `query`, `query_one`), `RealDictCursor`. Also `execute()` (2026-08-12) — the one write path, added specifically for `employees.py`.
- `mcp_server/server.py` — the 17 read-only tool implementations, plus a stdio-transport `Server` instance for local use (Claude Code CLI / Claude Desktop).
- `mcp_server/employees.py` (2026-08-12) — the employee-directory tool set: `list_employees`, `search_employees`, `get_employee` (read) + `create_employee`, `update_employee`, `deactivate_employee`, `reactivate_employee` (write) — this server's first-ever write-capable tools. See §5.1 below.
- `mcp_server/http_server.py` — Starlette app wrapping the tools for SSE/remote-connector access (claude.ai's "Add custom connector").
- `mcp_server/redaction.py` — the query-time redaction safety net (see below), independent of and in addition to the SQL-level tier filtering.

**The clearance-scoped identity pattern**: `build_server(clearance, name=..., extra_redact_categories=())` builds a *fresh* `mcp.server.Server` instance every call, with tier-filtered tools bound to a specific `clearance` list via `functools.partial` **at construction time**. `clearance` is never part of any tool's JSON `inputSchema` — nothing a caller sends can widen its own access. Three additive tier levels underlie everything:

```
FULL_CLEARANCE    = ["tier1", "tier2_confidential", "tier2"]
HR_CLEARANCE      = ["tier2_confidential", "tier2"]
GENERAL_CLEARANCE = ["tier2"]
```

`http_server.py` creates **four separate `Server` instances**, one per identity, each mounted at its own long-random-secret URL path segment — the URL path itself is the credential (no OAuth, no auth header). As of 2026-08-11, `general` was widened from tier2-only to the same DB-level clearance as `hr`, with two content-based redaction categories layered on top instead — see `mcp_server/redaction.py` for the category definitions:

| Identity | Secret env var | Clearance | Extra redaction | Employee tools? |
|---|---|---|---|---|
| `full` | `MCP_URL_SECRET` | `FULL_CLEARANCE` | none | Yes (read + write) |
| `hr` | `MCP_HR_URL_SECRET` | `HR_CLEARANCE` | `non_payroll_monetary_amounts` — every dollar figure stripped *except* payroll/salary/incentive | Yes (read + write) |
| `general` | `MCP_GENERAL_URL_SECRET` | `HR_CLEARANCE` | `monetary_amounts` (every dollar figure, no exceptions) + `employee_activity_monitoring` (Cattr/performance content) | No |
| `intern` | `MCP_INTERN_URL_SECRET` | `GENERAL_CLEARANCE` | `monetary_amounts` (every dollar figure, no exceptions) | No |

### 5.1 Employee directory tools (2026-08-12) — the first write path

`mcp_server/employees.py`'s 7 tools are gated onto `full` and `hr` **only**, via an `enable_employee_tools` flag on `build_server()` that is deliberately independent of `clearance` — `general` shares `HR_CLEARANCE`'s clearance *list* with `hr` for every other tool, but must not get employee access, so gating had to be a separate axis, not a clearance check. `employees` has no `access_tier` column at all; this table sits outside the tiered-content system entirely (see `docs/postgres-database.md` §3).

Write tools (`create_employee`/`update_employee`/`deactivate_employee`/`reactivate_employee`) take a `changed_by` kwarg bound at server-construction time to the identity name (`"full"` or `"hr"`), the same pattern `clearance` already uses — never part of a tool's `inputSchema`, so nothing a caller sends can spoof who made a change. Every write is logged to `employee_change_log` (best-effort, matching `ingest_log.py`'s "a logging failure must never mask an otherwise-successful run" philosophy — the employee-table write itself is what has to succeed).

Deletion is soft-delete only (`deactivate_employee` sets `status='inactive'`, never a real `DELETE`) — `list_employees`/`search_employees` default to `status='active'` (current headcount) and take an explicit `status='inactive'`/`'all'` argument for historical/former-employee lookups.

One-off import: `loaders/import_employees_from_xlsx.py` merges EOXS's multi-sheet HR spreadsheet into one canonical row per person (deduped by name, then by shared email — catches same-person/different-spelling cases like "Dhrup" vs "Dhrup Kumar"). Deliberately excludes LinkedIn URLs, personal phone numbers, and — most importantly — a plaintext-password column present in the source sheet, never imported regardless of how this table gets used later.

A **5th** `Server` instance (`server.py`'s module-level `server = build_server(FULL_CLEARANCE)`) exists purely for local stdio transport — always full clearance, on the reasoning that anyone able to run this file locally already has raw `.env` Postgres credentials anyway.

**The redaction safety net** (`mcp_server/redaction.py`): runs on every tool call for every identity except `full`. Independent of the SQL-level tier filter — it inspects the actual text of what's about to be returned and strips anything matching a restricted category, catching cases where the original tier classification was wrong. Uses Sonnet (not Haiku), redacts by exact verbatim span removal (never regenerates text), fails closed on repeated API error. Every actual redaction is logged to `mcp_redaction_log` (schema 023) for follow-up.

**Transport**: SSE (`mcp.server.sse.SseServerTransport`), not Streamable-HTTP — a documented design choice, since an earlier Streamable-HTTP + Authorization-header attempt didn't fit what claude.ai's connector dialog exposes (only an OAuth Client Secret field, no generic header input). Routes per identity: `GET /mcp/<secret>/sse` and `Mount /mcp/<secret>/messages/`. Adding a real Streamable-HTTP endpoint (for non-claude.ai clients, e.g. a custom frontend) is planned but not yet built — see the DigitalOcean migration roadmap.

**Port**: `MCP_HTTP_PORT` (default **8091**), binds `127.0.0.1` only — reachable solely via the nginx `/mcp/` proxy.

**Tool count: 20** tiered/read-only tools, present for every identity:
`get_index`, `get_wiki_page`, `search_wiki`, `list_emails`, `search_emails`, `get_email`, `get_attachment_text`, `list_calls`, `search_calls`, `get_call`, `list_assets`, `search_assets`, `get_asset`, `list_clients`, `list_contacts`, `get_client_profile`, `get_client_file`, `list_implementation_tasks`, `search_implementation_tasks`, `get_implementation_task`.
(`list_assets`/`search_assets`/`get_asset` added 2026-08-12 alongside the new `assets` table — see `docs/raw-ingestion.md` §2 Assets. `get_asset` returns the full raw document; the corresponding wiki page under `wiki/sources/assets/` is a synthesized summary, not a substitute for the original text.)
Plus **7 employee-directory tools** (§5.1), present only for `full`/`hr` — **27 tools total** for those two identities, 20 for `general`/`intern`.

`get_ticket`/`search_tickets`/`get_invoice`/`search_invoices` were **removed entirely** (2026-08-10) — support tickets and invoices/sales-orders are no longer part of this system's tool surface at all; that data now lives only in the separate `eoxs-teams` Odoo connector. The underlying `tickets`/`sales_orders`/`invoices` tables still exist in the schema (historical rows were deleted, not the tables themselves) — see `docs/postgres-database.md` and the known gap noted in `docs/raw-ingestion.md` §12.

Most tools are tier-filtered (all except `list_clients`/`list_contacts`, which have no `access_tier` column). A not-found response is deliberately indistinguishable from an access-denied one across every identifier-lookup tool — a lower-clearance caller can't use the difference to confirm a restricted record's existence. `get_client_profile` is a notable aggregator, pulling contacts/tasks/emails/calls and wiki counts for one client in a single call, all identically clearance-filtered.

**Known current-state gap**: `eoxs_readonly` (a genuinely read-only Postgres role) exists and is correctly scoped, but the MCP server does **not** currently connect as it — `mcp_server/db.py` reads the same `.env` `PGUSER`/`PGPASSWORD` as everything else, which is `eoxs_app` (full read-write). The "read-only" guarantee today is a code-discipline convention (every tool only issues SELECTs), not a database-enforced one. Switching the MCP server to connect as `eoxs_readonly` would close this gap and is a reasonable near-term hardening task.

## 6. Dependencies (`requirements.txt`)

Python **3.12.3** (system interpreter and `.venv` match).

| Package | Purpose |
|---|---|
| `psycopg2-binary` | PostgreSQL driver |
| `python-dotenv` | loads `.env` |
| `PyYAML` | YAML parsing |
| `mcp==1.29.0` | Model Context Protocol server/client library (exact-pinned) |
| `google-auth-oauthlib`, `google-api-python-client` | Gmail OAuth + API client |
| `anthropic` | Claude API SDK (classification + synthesis) |
| `httpx` | async HTTP client |
| `fastapi`, `uvicorn` | ingestion server + ASGI runtime |
| `svix` | webhook signature verification (Fathom) |
| `beautifulsoup4` | HTML parsing |
| `google-cloud-pubsub` | Gmail push-notification subscription client |

60 packages total resolve inside `.venv` once transitive dependencies (starlette, pydantic 2.x, the grpc/protobuf stack for pubsub, cryptography, jsonschema, etc.) are included.

## 7. Total code size (current, `wc -l`)

| Directory | Lines |
|---|---|
| `ingestion/` | 4,064 (23 files) |
| `mcp_server/` | 822 |
| `wiki_ingestion/` | 3,079 |
| `loaders/` | 1,188 |
| `parsers/` | 365 |
| **Total (Python)** | **9,518** |
| `schema/` (SQL, not counted above) | 846 lines across 22 numbered migrations |

## 8. Environment variables (names only — never commit or share actual values)

**Ingestion server / webhooks:** `INGESTION_SERVER_PORT`, `INGESTION_WEBHOOK_SECRET`, `FIREFLIES_WEBHOOK_SECRET`, `FATHOM_WEBHOOK_SECRET`

**Postgres:** `PGHOST`, `PGPORT`, `PGDATABASE`, `PGDATABASE_STAGING`, `PGUSER`, `PGPASSWORD`

**MCP server:** `MCP_URL_SECRET`, `MCP_HR_URL_SECRET`, `MCP_GENERAL_URL_SECRET`, `MCP_INTERN_URL_SECRET`, `MCP_HTTP_PORT`

**Anthropic:** `ANTHROPIC_API_KEY` (spam/relevance filters), `CLASSIFIER_ANTHROPIC_API_KEY` (tier classification — deliberately separate for independent cost tracking)

**Gmail** (one OAuth triplet per account): `RAJ_GMAIL_CLIENT_ID/SECRET/REFRESH_TOKEN`, `RON_GMAIL_CLIENT_ID/SECRET/REFRESH_TOKEN`, `REMYA_GMAIL_CLIENT_ID/SECRET/REFRESH_TOKEN`

**Zoho:** `ZOHO_CLIENT_ID`, `ZOHO_CLIENT_SECRET`, `ZOHO_REFRESH_TOKEN`

**Fireflies / Fathom:** `FIREFLIES_API_KEY`, `RON_FATHOM_API_KEY`

**Odoo** (one pair per tenant): `GREER_ODOO_USERNAME/PASSWORD`, `ESS_ODOO_USERNAME/PASSWORD`, `DPS_ODOO_USERNAME/PASSWORD`, `PPC_ODOO_USERNAME/PASSWORD`, `THREEGM_ODOO_USERNAME/PASSWORD`, `SABRE_ODOO_USERNAME/PASSWORD`, plus `EOXS_TICKETS_ODOO_USERNAME/PASSWORD` (the central tickets+invoices instance)

**Linear:** `LINEAR_EDB_API_KEY`, `LINEAR_EDB_TEAM_KEY` (the ones actually read by `linear_report.py` — see the note in `docs/linear-integration.md` about two *stale, unused* look-alike names, `LINEAR_API_KEY`/`LINEAR_TEAM_KEY`, that also exist in `.env` but aren't what the code reads)

**pgweb:** `PGWEB_DB_PASSWORD`, `PGWEB_HTTP_PASSWORD`

**wiki_ingestion internals:** `WIKI_CYCLE_ID`, `WIKI_SOURCE_KIND` (set per sub-agent invocation, not meant to be set manually)

## 9. Where this fits in the overall system

The backend server is the one physical machine everything else in this document set lives on: it hosts the database (`docs/postgres-database.md`), runs the raw-ingestion fetchers and their schedule (`docs/raw-ingestion.md`), runs the wiki-synthesis pipeline (`docs/wiki-ingestion.md`), and is where the Linear-reporting code executes from (`docs/linear-integration.md`). It also hosts the sibling `eoxs-frontend-threads` system (§2) and a Claude Code CLI / Codex CLI environment used directly for admin and development work (including a persistent `claude --teleport` session) — a real, load-bearing use of this being a full VPS, not just a place to run services, and the main reason a migration to a pure container-platform (see the DigitalOcean migration roadmap) can't simply move everything off it. Nothing in this system runs anywhere else — there's no separate worker fleet, no managed cloud database, no serverless functions. One VPS, seven `eoxs-wiki-db` services plus one sibling-repo service, one Postgres instance.
