# Backend Server

*Deep technical reference for anyone being onboarded to backend-server work on Cruz (`eoxs-wiki-db`).*

## 1. Where this runs

One VPS: hostname `ubuntu-4gb-sin-1`, reachable at bare IP `5.223.44.95` — **no domain name**. Repo lives at `/home/deploy/eoxs-wiki-db`. Every application service runs as Linux user `deploy` (uid 1000). Note: `deploy` is also in the `sudo` group — if any of these services were ever compromised, the attacker would inherit sudo-capable access, not a locked-down service account. Tightening this (a dedicated non-sudo service user) is a reasonable hardening task for a future developer, not something currently done.

## 2. What's running here, service by service

All app-level systemd units live in `deploy/` in the repo (installed as `/etc/systemd/system/*`). Every unit sets `User=deploy`, `Group=deploy`, `WorkingDirectory=/home/deploy/eoxs-wiki-db`, and logs to journald (`StandardOutput=journal`, distinct `SyslogIdentifier` per unit — there are no separate log files; `journalctl -u <unit>` is the only place to look).

**Per-service scoped env files (2026-09-04), not one shared `.env`**: every unit used to read the same `EnvironmentFile=/home/deploy/eoxs-wiki-db/.env`, meaning e.g. the MCP server's process environment carried every Gmail/Zoho/Odoo/Linear credential it never uses, and — the real motivating incident, see §7 — the wiki-pipeline's `claude -p` subprocess inherited `ANTHROPIC_API_KEY` purely because it was present service-wide. Each unit now gets its own minimal `.env.<service>` file, containing only the variables that service actually reads:

| Unit | Env file | Contains |
|---|---|---|
| `eoxs-ingestion.service`, `eoxs-sweep.service` | `.env.ingestion` | Postgres + every raw-source credential (Gmail/Zoho/OAuth, Fireflies, Fathom) + `ANTHROPIC_API_KEY`/`CLASSIFIER_ANTHROPIC_API_KEY` + Linear |
| `eoxs-mcp.service` | `.env.mcp` | Postgres + `CLASSIFIER_ANTHROPIC_API_KEY` (redaction) + all 6 `MCP_*_URL_SECRET` values — no Gmail/Zoho/Odoo/Linear credentials at all |
| `eoxs-wiki-mcp.service` | `.env.wiki-mcp` | Postgres only |
| `eoxs-wiki-pipeline.service` | `.env.wiki-pipeline` | Postgres + Linear (no Anthropic API keys — see §7's incident; the `claude -p` subprocess env is scrubbed further still, at the Python level, not just by what's in this file) |
| `pgweb.service` | `.env.pgweb` | `PGWEB_DB_PASSWORD`, `PGWEB_HTTP_PASSWORD` |
| `pgweb-staging.service` | `.env.pgweb-staging` | same shape as above, staging-scoped |

`.env` itself still exists (used by local/manual script invocations, e.g. running a fetcher by hand from a shell) but is no longer what any systemd unit reads. This is real secret-scoping, not just an organizational tidy-up — the MCP server process, for example, can no longer read a single raw-ingestion source credential even if a bug tried to.

| Unit | Type | What it runs | Cadence |
|---|---|---|---|
| `eoxs-ingestion.service` | simple, `Restart=always` | `python3 -m ingestion.server` | always-on |
| `eoxs-mcp.service` | simple, `Restart=always` | `python3 -m mcp_server.http_server` | always-on |
| `eoxs-wiki-mcp.service` | simple, `Restart=always` | `python3 -m wiki_ingestion.mcp_http_server` | always-on — internal-only MCP server the wiki-pipeline's `claude -p` sub-agents connect to, never customer-facing |
| `eoxs-sweep.service` | oneshot | `python3 -m ingestion.server --sweep` | triggered by its timer |
| `eoxs-sweep.timer` | timer | triggers the above | `OnCalendar=00/2:00:00`, `RandomizedDelaySec=120`, `Persistent=true` — every 2 hours, ±2min jitter, catches up after a reboot |
| `eoxs-wiki-pipeline.service` | oneshot, `TimeoutStartSec=infinity` | `python3 -m wiki_ingestion.run_pipeline` | triggered by its timer |
| `eoxs-wiki-pipeline.timer` | timer | triggers the above | `OnCalendar=00/6:00:00`, `RandomizedDelaySec=120`, `Persistent=true` — every 6 hours |
| `eoxs-healthcheck.service` | oneshot, `User=root` | `deploy/healthcheck.sh` | triggered by its timer |
| `eoxs-healthcheck.timer` | timer | triggers the above | `OnCalendar=hourly`, `RandomizedDelaySec=300`, `Persistent=true` — see §2.1 |
| `pgweb.service` | simple, `Restart=always` | `pgweb --bind=127.0.0.1 --listen=8092 --url=postgres://eoxs_readonly@localhost:5432/eoxs_wiki --readonly --lock-session --auth-user=dbadmin ...` | always-on |
| `pgweb-staging.service` | simple, `Restart=always` | same as above, port **8095**, `--url=...@localhost:5432/eoxs_wiki_staging` | always-on, added 2026-08-14 |
| `nginx.service` | system-provided | reverse proxy (see §4) | always-on |

A sibling system, `eoxssecondbrain/eoxs-frontend-threads` (a separate repo, its own clone at `/home/deploy/eoxs-frontend-threads`, its own venv, its own `eoxs-frontend-threads.service`), runs on this same physical box but is **not** part of this repo's codebase — deliberately split out so raw frontend-chat-thread volume never bloats this system, same reasoning as `claude-notes-vault`'s split from `raj-wiki-vault`. It has its own dedicated `eoxs_frontend_threads` database on this same Postgres instance.

`eoxs-wiki-pipeline.service`'s `TimeoutStartSec=infinity` is deliberate — it runs sequential `claude -p` sub-agent calls that can take 3–5+ minutes per batch and hours for a full run, which would otherwise hit systemd's default ~90s oneshot timeout and get killed mid-run.

### 2.1 `eoxs-healthcheck.service` (2026-09-05) — catching failures uptime monitoring can't see

Added directly in response to two real incidents that plain "is the box up" monitoring would have missed entirely: the wiki-pipeline ran and failed on every single cycle for a full day with nothing alerting (the `claude` CLI binary was present but not logged in — the box, nginx, and every systemd unit's own exit code looked completely healthy the whole time), and separately a sweep source hit a per-account error that got caught and logged inside the summary but never failed the unit itself, so `systemctl` reported success while a mailbox silently stopped ingesting.

`deploy/healthcheck.sh` (installed as `eoxs-healthcheck.service`/`.timer`, hourly, `RandomizedDelaySec=300`) checks four things a bare uptime check cannot:
1. **Did each scheduled job run recently enough?** — `eoxs-sweep`'s and `eoxs-wiki-pipeline`'s last `ExecMainStartTimestamp` against a max age roughly 2x each timer's own cadence (5h / 13h), so ordinary `RandomizedDelaySec` jitter never trips a false alarm. A disabled timer is reported as a note, not a failure — some cut-over scenarios disable a timer deliberately, and alerting on that would train people to ignore the script.
2. **Did the last run actually succeed?** — `systemctl is-failed` on each service.
3. **Per-source errors that don't fail the unit** — greps the last 6 hours of `eoxs-sweep.service`'s journal for the `{'error': ...}` shape a caught-but-logged per-source fetcher exception leaves behind (see `ingestion/server.py`'s `run_full_sweep()`), since the unit itself still exits 0 in that case.
4. **The always-on services are actually active** — `eoxs-ingestion`, `eoxs-mcp`, `eoxs-wiki-mcp`, `eoxs-frontend-threads`, `nginx`, `postgresql`.

Exit 0 = healthy, 1 = a problem (with what, printed). An optional `HEALTHCHECK_PING_URL` (Healthchecks.io or equivalent, set in `/etc/eoxs-healthcheck.env`, not tracked in the repo) gets pinged only on a healthy run — the dead-man's-switch half, catching "the whole box is gone," which nothing running *on* the box can ever detect about itself. A failing run deliberately does not ping, so silence itself is the alert.

**Known secret-hygiene issue**: the `pgweb.service` unit interpolates `${PGWEB_DB_PASSWORD}` and `${PGWEB_HTTP_PASSWORD}` directly into its `ExecStart=` command line. That means both secrets are visible in plaintext to anyone able to run `systemctl status pgweb` or `ps` as *any* local user, not just root. Worth fixing (e.g. wrap the actual pgweb invocation in a small shell script that reads the env vars internally instead of interpolating them into the unit file) — flagged here rather than silently left for the next person to discover the hard way.

## 3. The Ingestion Server (`ingestion/server.py`, 324 lines)

FastAPI app, `title="eoxs-wiki-db Raw Ingestion Server"`. Port: `INGESTION_SERVER_PORT` env var (default **8090**), binds `0.0.0.0` — reachable only via nginx's reverse proxy in practice (see §4), not directly from the internet.

Routes:

| Route | Purpose | Auth |
|---|---|---|
| `GET /health` | `{status, run_in_progress, last_run}` | none |
| `POST /webhook/gmail` | Gmail Pub/Sub push (`{emailAddress, historyId}` base64 envelope) — refetches **all 3** Gmail accounts in the background, always returns 200 immediately | none (URL itself is the secret) |
| `POST /webhook/fireflies` | Fireflies `transcript.completed` webhook | HMAC-SHA256 via `FIREFLIES_WEBHOOK_SECRET`, checked only if the var is set |
| `POST /webhook/fathom` | Fathom `recording.completed` webhook | Svix-style HMAC via `FATHOM_WEBHOOK_SECRET`, checked only if the var is set |
| `POST /trigger/manual` | Manual full sweep across `run_full_sweep()`'s current 4-source list (Gmail, Zoho, Fireflies, Fathom); the only trigger path that reports to Linear | Bearer token against `INGESTION_WEBHOOK_SECRET`, if set |

A module-level `asyncio.Lock` (`_sweep_lock`) ensures only one run executes at a time — a duplicate trigger while one is in-flight is logged and dropped, not queued.

`run_full_sweep()`'s source list is 4, not 7 — the per-client Odoo implementation-board fetch, tickets, and invoices were each deliberately removed from it (2026-08-10/2026-08-12, see `docs/raw-ingestion.md` §2/§8), leaving only Gmail/Zoho/Fireflies/Fathom as ongoing, on-schedule sources. Zoho has **no webhook path at all** — it's only ever picked up by the 2-hourly sweep. See `docs/raw-ingestion.md` for the full per-source breakdown.

Also piggybacking on the same 2-hourly sweep call (not a separate trigger): `ingestion/redaction_linear_report.py`'s `report_new_redaction_events()` reports any new `mcp_redaction_log` rows (MCP query-time redaction events, §5.4/below) to Linear as a parent+child issue set — see `docs/linear-integration.md` §4a.

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
    - `location /dbadmin-staging/` → `proxy_pass http://127.0.0.1:8095/` (added 2026-08-14) — exact sibling of the above, browsing `eoxs_wiki_staging` instead via a separate `pgweb-staging.service` instance. Same auth_basic credentials, same three-layer read-only enforcement.
    - `location /` → `proxy_pass http://127.0.0.1:8090` (the ingestion server, catch-all).

`deploy/reload-nginx.sh` is installed as a certbot deploy-hook (`/etc/letsencrypt/renewal-hooks/deploy/`) — it runs `systemctl reload nginx` automatically after every successful renewal so nginx always serves the current cert. Automatic renewal itself is confirmed live via `snap.certbot.renew.timer`.

The ingestion server's logs show routine internet-scanner noise (`GET /mysql-admin/`, `/cgi-bin/snapshot.cgi`, etc., all 404) — expected background noise for any bare-IP HTTPS listener with no WAF, not a sign of compromise.

## 5. MCP Server (`mcp_server/`)

This is where the access-rights system is actually enforced. See `docs/postgres-database.md` for the `access_tier` column itself; this section is about the serving layer.

- `mcp_server/db.py` — thin psycopg2 helper (`get_conn`, `query`, `query_one`), `RealDictCursor`. Also `execute()` (2026-08-12) — the one write path, added specifically for `employees.py`.
- `mcp_server/server.py` — the 23 read-only tool implementations, plus a stdio-transport `Server` instance for local use (Claude Code CLI / Claude Desktop).
- `mcp_server/employees.py` (2026-08-12) — the employee-directory tool set: `list_employees`, `search_employees`, `get_employee` (read) + `create_employee`, `update_employee`, `deactivate_employee`, `reactivate_employee` (write) — this server's first-ever write-capable tools. See §5.1 below.
- `mcp_server/http_server.py` — Starlette app wrapping the tools for SSE/remote-connector access (claude.ai's "Add custom connector").
- `mcp_server/redaction.py` — the query-time redaction safety net (see below), independent of and in addition to the SQL-level tier filtering.

**The clearance-scoped identity pattern**: `build_server(clearance, name=..., extra_redact_categories=())` builds a *fresh* `mcp.server.Server` instance every call, with tier-filtered tools bound to a specific `clearance` list via `functools.partial` **at construction time**. `clearance` is never part of any tool's JSON `inputSchema` — nothing a caller sends can widen its own access. Four additive tier levels underlie everything (was three until 2026-09-02, see below):

```
FULL_CLEARANCE          = ["tier1", "tier2_confidential_hr", "tier2_confidential", "tier2"]
HR_CLEARANCE            = ["tier2_confidential_hr", "tier2_confidential", "tier2"]
INTERNAL_TEAM_CLEARANCE = ["tier2_confidential", "tier2"]
GENERAL_CLEARANCE       = ["tier2"]
```

**2026-09-02: `tier2_confidential_hr` split out of `tier2_confidential`** (schema/035_tier2_confidential_hr.sql) — employee-facing HR/financial content (payroll/salary/compensation/incentive/bonus, onboarding/offboarding, disciplinary action, sensitive credentials), judged by actual substance rather than any keyword or self-declared "Confidential" label. Before this, `general` (internal team) shared `HR_CLEARANCE` **outright** with `hr` — the *only* thing keeping payroll/HR content out of `general`'s responses was the query-time redaction layer stripping it after the row was already visible pre-redaction. Now `general` has its own `INTERNAL_TEAM_CLEARANCE` (the old pre-split `tier2_confidential + tier2` list), which structurally excludes the new tier — HR content never reaches `general`'s context at all, not just redacted out of it. The redaction categories (`monetary_amounts`, `employee_activity_monitoring`) remain layered on top of `general`'s clearance as a fallback safety net, for tier2_confidential/tier2 content that mentions money/monitoring data for reasons unrelated to the HR carve-out (e.g. client billing). See `ingestion/reclassify_hr_tier.py` for the one-time re-classification this required across every table — 1,130 wiki pages, both `assets` rows then at tier2_confidential, and all five email accounts, not just Isha's (HR) mailbox, matching the same full-content-read audit that also caught a pre-existing false positive (below).

**Same pass also caught a real misclassification, unrelated to the HR split**: the `eoxs-client-implementation-go-live-sop` asset had been sitting at `tier2_confidential` purely because its own front-matter carries an "Internal and Confidential" label — a generic SOP-template convention, not evidence of actual confidential content (no payroll, financial, legal, or vendor-pricing detail anywhere in the document). Reclassified to `tier2`. Both classifier prompts (`ingestion/inline_tier_classifier.py`, `ingestion/tier_classifier.py`, `wiki_ingestion/tier_classifier.py`) now explicitly instruct "judge by actual content, never by a document's own self-declared label" to prevent this recurring.

`http_server.py` creates **six separate `Server` instances**, one per identity, each mounted at its own long-random-secret URL path segment — the URL path itself is the credential (no OAuth, no auth header). As of 2026-08-11, `general` was widened from tier2-only to the same DB-level clearance as `hr`, with two content-based redaction categories layered on top instead (2026-09-02: `general`'s clearance constant changed as described above, but its actual access — tier2_confidential + tier2, plus the same two redaction categories — is unchanged by that; it only lost the newly-split-out HR tier) — see `mcp_server/redaction.py` for the category definitions:

| Identity | Secret env var | Clearance | Extra redaction | Employee tools? | Asset write? | Database |
|---|---|---|---|---|---|---|
| `full` | `MCP_URL_SECRET` | `FULL_CLEARANCE` | none | Yes (read + write) | create + update, any slug | live |
| `ayan` | `MCP_AYAN_URL_SECRET` | `FULL_CLEARANCE` | none | Yes (read + write) | create + update, any slug | live |
| `hr` | `MCP_HR_URL_SECRET` | `HR_CLEARANCE` | `non_payroll_monetary_amounts` — every dollar figure stripped *except* payroll/salary/incentive | Yes (read + write) | update only, `eoxs-salary-details` only | live |
| `general` | `MCP_GENERAL_URL_SECRET` | `INTERNAL_TEAM_CLEARANCE` | `monetary_amounts` (every dollar figure, no exceptions) + `employee_activity_monitoring` (Cattr/performance content) | No | No | live |
| `intern` | `MCP_INTERN_URL_SECRET` | `GENERAL_CLEARANCE` | `monetary_amounts` (every dollar figure, no exceptions) | No | No | live |
| `staging_qa` | `MCP_STAGING_URL_SECRET` | `FULL_CLEARANCE` | none | Yes, unrestricted | Yes, unrestricted | **staging** |

**`ayan`** (added 2026-08-24, landed in commit `f6e01e9` folded into an unrelated container-platform-deployability commit — worth knowing if you're `git blame`-ing this) is a second full-clearance, write-capable identity, functionally identical to `full` in every permission respect. It exists so Ayan's own writes are tagged `changed_by='ayan'` in `employee_change_log`/`asset_change_log` rather than `changed_by='full'`, for clean attribution — not because he needs different access than Raj. **This identity caused a real production outage on 2026-08-25**: `MCP_AYAN_URL_SECRET` is a hard-required `os.environ[...]` lookup (not `.get()` with a default), but was never added to this droplet's `.env` when the identity was added. `eoxs-mcp.service` had been running since before this code landed, so the gap was silently masked for a day until the service was restarted for an unrelated reason, at which point it crash-looped on `KeyError: 'MCP_AYAN_URL_SECRET'` until the secret was added. **Lesson**: adding a new hard-required env var to a long-lived service's code is a latent outage until that service's next restart — grep `os.environ["..."]` (not `.get`) across a module before assuming a code change is safe to leave undeployed.

### 5.1 Employee directory tools (2026-08-12) — the first write path

`mcp_server/employees.py`'s 7 tools are gated onto `full` and `hr` **only**, via an `enable_employee_tools` flag on `build_server()` that is deliberately independent of `clearance` — before 2026-09-02, `general` shared `HR_CLEARANCE`'s clearance *list* with `hr` outright for every other tool, but still had to be denied employee access, so gating had to be a separate axis, not a clearance check; `general` now has its own `INTERNAL_TEAM_CLEARANCE` (see §5 above), but employee-tool gating remains its own independent axis regardless. `employees` has no `access_tier` column at all; this table sits outside the tiered-content system entirely (see `docs/postgres-database.md` §3).

Write tools (`create_employee`/`update_employee`/`deactivate_employee`/`reactivate_employee`) take a `changed_by` kwarg bound at server-construction time to the identity name (`"full"` or `"hr"`), the same pattern `clearance` already uses — never part of a tool's `inputSchema`, so nothing a caller sends can spoof who made a change. Every write is logged to `employee_change_log` (best-effort, matching `ingest_log.py`'s "a logging failure must never mask an otherwise-successful run" philosophy — the employee-table write itself is what has to succeed).

Deletion is soft-delete only (`deactivate_employee` sets `status='inactive'`, never a real `DELETE`) — `list_employees`/`search_employees` default to `status='active'` (current headcount) and take an explicit `status='inactive'`/`'all'` argument for historical/former-employee lookups.

### 5.2 Asset writes (2026-08-13) — the second and last write path

`mcp_server/asset_writes.py` adds `create_asset`/`update_asset` for the `assets` table (schema/031, docs/raw-ingestion.md §2) — curated internal reference documents: SOPs, company overview, ICP, salary register, product-feature specs, technical references. Per explicit instruction, this and the employee directory (§5.1) are the **only** two write surfaces anywhere in this server — no other table has, or is planned to have, a write path.

Gating is per-tool, not just per-identity, via `asset_write_scope` on `build_server()`:
- `full`: `asset_write_scope="all"` — both `create_asset` and `update_asset`, any slug.
- `hr`: `asset_write_scope={"eoxs-salary-details"}` — `update_asset` **only**, and only for that one slug (a constant, `SALARY_ASSET_SLUG`, in `http_server.py`). `create_asset` is never bound for a restricted scope at all — adding a brand-new document is `full`-only, not something a slug allowlist could safely narrow. Calling `update_asset` with any other slug returns a plain permission error (`{"error": "this connection cannot write to asset '<slug>' -- only [...] is permitted"}`), not a partial write or silent no-op.
- `general`/`intern`: `asset_write_scope=None` — neither tool exists on these connections at all.

`create_asset` computes `access_tier` automatically via the same `inline_tier_classifier.classify_tier()` call the one-time import (`ingestion/import_assets.py`) uses — it is never a caller-supplied argument, so a mis-tiered sensitive document can't be created as `tier2` by mistake. `update_asset` does **not** reclassify `access_tier` on edit, matching the "tier survives every refresh" convention every raw-ingestion writer in this codebase already follows (docs/raw-ingestion.md §3) — a genuine sensitivity change is a deliberate manual reclassification, not a side effect of an ordinary content edit. **2026-09-02: `eoxs-salary-details` reclassified from `tier2_confidential` to `tier2_confidential_hr`** (see §5 above) — `hr`'s write restriction to this one slug was never about which tier it sits at, so is unaffected; what changed is that `general` can no longer read this asset at all (previously readable, with payroll figures redacted at query time).

Every write is logged to `asset_change_log` (schema/032) — full old/new title and body text per change, not just field-level diffs (unlike `employee_change_log`; these are a "handful of long documents," per schema/031's own comment, so a real version history is affordable and, for the salary register specifically, valuable in its own right). `get_asset` surfaces a lightweight `change_history` (who, what kind of change, when) inline; the full before/after text lives in `asset_change_log` itself, not repeated on every `get_asset` call.

**Why no extra pipeline work was needed to keep the wiki fresh**: `wiki_ingestion/detect.py`'s `candidates_assets()` (built 2026-08-12 for the one-time backfill) already watches `assets.updated_at` against a stored cursor and content-hashes the result — it has no way to distinguish "changed by a script" from "changed by a live tool call." A `create_asset`/`update_asset` call is simply a new way to advance `updated_at` on a row that partition was already watching. The next scheduled `eoxs-wiki-pipeline.timer` run (every 6 hours) picks up any asset write automatically and re-drafts the corresponding wiki page — exactly the behavior asked for, with zero changes to `wiki_ingestion/`.

**`search_assets` ranking (2026-08-14)** — real UX gap found live: a vague write-target reference ("AI Joe product SOP," no asset literally titled that) returned an unordered, un-scored candidate list, giving the calling model no way to distinguish a strong match from a coincidental one — it had to stop and ask every time, even when one candidate was clearly the intended one. Switched from plain `ILIKE` to `similarity(title, query)` (`pg_trgm`, `idx_assets_title_trgm` — schema/033) plus a small additive bonus (`+0.15`, capped at 1.0) when the query also appears in the body — additive, not a replacement floor, specifically because a flat floor was tried first and flattened every body-containing document to an identical score for a short/generic query (e.g. "SOP" matched 10 of 15 documents' bodies, all tying). Every result now carries `match_score` (0–1); the skill files instruct the model to proceed on a clearly-leading score and only ask (as a numbered list, for a fast reply) when top scores are genuinely close.

### 5.3 `staging_qa` (2026-08-13) — a QA sandbox for the two write paths above, isolated by database, not just by data

Both write features above (§5.1, §5.2) needed a way to test real create/update/delete behavior — does the model create the right row, does it ever touch something it shouldn't, does an update actually take — without any of that risking live data or getting swept into the wiki. `staging_qa` is a 5th HTTP identity that gets full clearance and every tool, read and write, completely unrestricted (there's nothing to protect in disposable test data) — but with one difference from every other identity: **every single tool call it makes is transparently routed to the `eoxs_wiki_staging` database instead of live `eoxs_wiki`.**

**Mechanism** (`mcp_server/db.py`): a `contextvars.ContextVar` (`_target_database`), defaulting to unset (live, unchanged behavior for every other identity). `build_server()` takes a new `database` param (`None` or `"staging"`); its `call_tool()` handler wraps each dispatched tool call in `with use_database(database):` before invoking it. Because every existing tool function — all 23 read tools, `employees.py`'s 7, `asset_writes.py`'s 2 — already funnels through `mcp_server/db.py`'s `query()`/`query_one()`/`execute()`, this required editing **only `db.py` and the one wrapper line in `call_tool()`** — zero changes to any individual tool function's signature or body.

**Why a write through this identity can never reach the wiki, structurally, not just by policy**: `wiki_ingestion/` has no code path to the physical `eoxs_wiki_staging` database at all — confirmed by grep, zero references to `get_staging_conn`/`PGDATABASE_STAGING` anywhere in that directory. (This is a different "staging" than the `wiki_staging` *schema* draft-review pages sit in before promotion, §9 of `docs/postgres-database.md` — that one lives inside live `eoxs_wiki` itself; `eoxs_wiki_staging` is an entirely separate physical database, historically used only for raw-ingestion fetcher testing via `dual_write()`, see `docs/raw-ingestion.md` §4.) Since the wiki pipeline only ever calls `ingestion.db.get_live_conn()`, a row that only ever existed in `eoxs_wiki_staging` is simply invisible to it — not filtered out, not excluded by a flag, just unreachable.

**Distinguishing QA writes and cleaning up**: every write through this identity is tagged `changed_by='staging_qa'` in `employee_change_log`/`asset_change_log` (the same construction-time-bound `changed_by` mechanism §5.1/§5.2 already use), so `SELECT * FROM employee_change_log WHERE changed_by='staging_qa'` (run against staging) shows exactly what a QA session wrote. For actually resetting the sandbox, `loaders/reset_staging_qa_data.py --commit` is a full wipe-and-remirror: it truncates staging's `employees`/`assets`/both change-log tables and re-copies every row (preserving `id`) from live — a clean, deterministic baseline regardless of what QA did (new fabricated rows, edited existing ones, anything), rather than trying to mechanically replay each change log entry backwards. It only ever reads from live and writes to staging; verified live-untouched by direct query before this went live.

Deliberately **not** documented in any of the four main skill files (`deploy/eoxs-wiki-db-skill*.md`) — this identity is for direct hands-on testing by whoever holds the secret, not a persistent claude.ai connector meant to answer real questions. See `deploy/eoxs-wiki-db-skill-staging-qa.md` instead.

### 5.4 The `_environment` field (2026-08-14) — closing a real trust gap the above left open

**Real incident, not a hypothetical**: a `staging_qa` test session, connected to the correct secret with no confusion server-side, had **no skill file attached** in that particular claude.ai conversation. With no prompt-level context telling it this was the sandbox, and nothing in any tool's *result* saying so either, the model reasoned from caution alone that it must be talking to production, and nearly asked the user to approve a write it believed would hit live. Verified directly against both databases afterward: nothing was actually written anywhere, at any point, in that session — but the model's *belief* was wrong, and every prior tool result gave it zero factual basis to know better. The whole "which environment am I in" signal lived entirely out-of-band, in skill-file text a user could forget to attach or attach the wrong one for — exactly what happened.

**Fix**: every result from a write tool (`create_employee`/`update_employee`/`deactivate_employee`/`reactivate_employee`/`create_asset`/`update_asset`, across every identity — `WRITE_TOOL_NAMES` in `server.py`) gets an `_environment` field stamped onto it inside `call_tool()`, after redaction (so it's never at risk of being touched by that step) and regardless of success or error (an error result gets stamped too — even a *rejected* write, like `hr` attempting a disallowed slug, correctly reports which database it almost touched). Server-asserted, built from the same `database`/`identity_name` values already bound at construction time — never something a caller's arguments or a skill file's wording could spoof or omit. Deliberately scoped to write tools only, not all 32 — this is where a wrong belief has real stakes; the 23+ read tools were left unchanged to avoid any risk of breaking an existing skill file's documented response shape for a problem reads don't actually have.

All 3 skill files whose identity can write (`eoxs-wiki-db-skill.md`, `eoxs-wiki-db-skill-hr.md`, `eoxs-wiki-db-skill-staging-qa.md`) now explicitly tell the model to trust this field over its own assumption, and to treat an unexpected value (e.g. `staging_qa` ever seeing anything but `"STAGING..."`) as a serious problem worth stopping and flagging, not something to reason past either way.

### 5.5 `repo_docs` (2026-08-26) — this repository's own docs/architecture/codebase, made queryable

`mcp_server/repo_docs.py` adds `list_repo_docs`/`search_repo_docs`/`get_repo_doc` for a new `repo_docs` table (schema/034) — every `docs/*.md` file (except `docs/training/`), `ARCHITECTURE.md`, and one synthesized codebase-overview document with no single source file of its own. Read-only, no write tools — the only two write-capable tables in this server remain `employees` (§5.1) and `assets` (§5.2); this is deliberately not a third.

Unlike `assets`, `access_tier` is **not** classified per-document — every row is hardcoded `'tier1'` at import time (`ingestion/import_repo_docs.py`), since the whole category (credentials layout, infra topology, schema internals, redaction logic, per-identity secrets) is internal engineering/ops detail with no reason to be visible past `full`/`ayan`. The three tools are present on every identity's tool list (same as any other tiered tool — see the tool count in §5 above) but structurally return nothing for `hr`/`general`/`intern`/`staging_qa`, the same SQL-level `access_tier::text = ANY(clearance)` filter every other tiered tool already uses, not a separate access-control path.

No change-log table — this isn't a live-edited table the way `assets` is. **2026-08-28: `ingestion/import_repo_docs.py`'s `import_all()` now runs automatically at the start of every `wiki_ingestion/run_pipeline.py` cycle** (every 6 hours, wrapped in try/except so an importer bug can never block real ingestion) — previously it only ran when someone remembered the manual command, which meant a doc edit had no path into the wiki at all until that happened. It's still safe to run by hand too (`python -m ingestion.import_repo_docs`, upserting, idempotent on unchanged content). Search uses the same `similarity()`-ranked trigram approach as `search_assets` (§5.2's ranking note) via `idx_repo_docs_title_trgm`.

**Wired into wiki-ingestion detection, same pattern as `assets` (§5.2):** `wiki_ingestion/detect.py`'s `candidates_repo_docs()` watches `repo_docs.updated_at` against a `wiki_ingest_` cursor exactly like `candidates_assets()` does, registered as its own `repo_docs` partition in `run_detection()`/`build_all_candidates()`. Because the importer now runs first in every cycle, a `docs/*.md`/`ARCHITECTURE.md` edit is *meant* to flow all the way to a synthesized, cited wiki page automatically, no manual step anywhere in between — but see §5.7: from launch until 2026-09-03 this never actually worked, only detection did. `run_agent.py`'s `_row_summary()` has a `repo_docs` case (`repo doc id=... slug=...: title`) alongside `assets`'/`tickets`'/`calls`' existing ones.

### 5.6 `list_emails` sorted on the wrong column (found and fixed 2026-09-02) — silently hid every current email behind mid-August legacy rows

`list_emails` (§5's tool list) ordered by `source_file_path DESC` and filtered its `month` argument with `source_file_path LIKE '%/month/%'`. That column is **only ever set on the ~17.4k rows from the legacy file-based import** (`get_email`'s own docstring already said this) — every API-ingested Gmail/Zoho thread since (currently 293 on `raj_gmail` alone) has `source_file_path IS NULL`. Sorting a string column `DESC` puts NULLs last, so `list_emails` returned only legacy rows, and among those, the lexicographically-highest path string — which tops out in mid-August, never reaching any API-ingested thread no matter how recent. A caller using only `list_emails` concluded raw ingestion for `raj_gmail` had silently stopped around 2026-08-17; `search_emails`/`get_email` (correctly untouched by this bug) proved the same threads existed through 2026-09-02 and were already wiki-cited — the bug was in this one read path, not in ingestion.

Fixed by sorting/filtering on `(SELECT max(d) FROM unnest(thread_dates) d)` — the actual latest message timestamp in the thread — instead of the legacy path string. `search_emails`/`get_email`/`list_calls`/`search_calls`/`get_call` were already correct (`list_calls` already sorts on `call_date DESC NULLS LAST`); this was specific to `list_emails`.

### 5.7 `repo_docs` MCP tools defaulted `clearance=None`, silently blocking wiki-ingestion of all 14 docs (found and fixed 2026-09-03)

`mcp_server/repo_docs.py`'s three functions defaulted `clearance=None`, unlike every other tiered tool in `mcp_server/server.py` (which default to `FULL_CLEARANCE`). This was invisible from the real customer-facing MCP server — `build_server()` always binds a real `clearance` via `functools.partial` for every name in `TIER_FILTERED_TOOLS` (§5.6 lists it), `list_repo_docs`/`search_repo_docs`/`get_repo_doc` included, so `full`/`ayan` could always query these tools correctly through claude.ai. The break was specific to `wiki_ingestion/agent_mcp_server.py` — the internal MCP server the headless `claude -p` synthesis sub-agent connects to — which reuses `mcp_server.server.TOOLS` unwrapped (`func(**arguments)`, no `functools.partial`, no clearance override at all) for every read tool. With `clearance=None`, `access_tier::text = ANY(NULL)` evaluates to `NULL` (never true) in Postgres, so the sub-agent's `list_repo_docs` always returned `[]` and `get_repo_doc` always returned "not found," for all 14 rows, every cycle, since the table launched (2026-08-26).

Two real batches ran under this bug (cycle 108, 2026-08-28, 14 rows; cycle 129, 2026-09-02, 4 rows) and both logged `status: done` at the ingest stage — but produced **zero** wiki pages from `repo_docs`, because the sub-agent correctly detected every read tool failing, refused to fabricate pages from candidate titles alone (per its own instructions), and said so in its final plain-text summary — which was never persisted anywhere (no transcript logging on this path) or surfaced to a human. `wiki_ingest_seen`'s `ON CONFLICT DO UPDATE` also means only the latest per-row decision survives, so the visible symptom by the time this was investigated was just `decision: skipped_unchanged` — indistinguishable, from that table alone, from "correctly unchanged since last time." `wiki_ingest_batches` (append-only, never overwritten) was the only place that still showed both historical attempts.

Fixed by defaulting all three functions to `FULL_CLEARANCE` (mirroring every other tool in `server.py`) — matches the fact that this whole table is hardcoded tier1 regardless of caller, so there was never a real reason for a narrower default. Verified live: re-ran the `repo_docs` batch against `wiki_staging` after the fix and confirmed the sub-agent drafted all 14 pages, each correctly cited with `source_type='repo_doc'`.

**Lesson for future audits**: a batch/cycle status of `done` describes the *ingest* stage only — it says candidate rows were accepted into a batch, not that a sub-agent successfully drafted anything from them. Check `wiki_citations` (or the promotion list in the pipeline's own JSON summary) for a source category actually landing pages, not just its batch status.

**Lesson for auditing this system**: a "no recent data" finding from a single list/search tool is not proof of an ingestion gap — cross-check with a second tool (or a direct query) before concluding the pipeline is broken, since a read-path bug and a real ingestion gap look identical from one tool's output alone.

### 5.8 Wiki-first retrieval + citation-hopping (2026-09-08) — `wiki_citations` (4,510 rows) was populated by ingestion but never read by any live-serving tool

**Real incident that surfaced this**: a user asked to pull up a specific email about a PS Data integration request. A wiki page (id 1945, "Raj Requests PS Data CSX API Access via Tripp Collier", correctly cited to `email_thread:67695`) already existed and was the exact right answer — but the calling model never checked the wiki. It called `search_emails` twice with multi-word queries (implicit-AND full-text search, so a query naming several entities returned `[]` the moment no single thread contained every word), then `list_emails` with the wrong month range, and never found the thread. Two separate gaps, both fixed together:

**Gap 1 — `search_wiki`/`get_wiki_page` never read `wiki_citations` at all.** Even when a model *did* check the wiki and got a hit, the response carried no way to jump to the exact raw record the page was drafted from — only `title`/`page_type`/`snippet` (search_wiki) or the page body plus `wiki_links`/`wiki_flags` (get_wiki_page), never citations. A caller had to re-search raw data blind to "verify" or get full text, defeating the point of having a synthesized, cited answer. Fixed: both tools now join `wiki_citations` and return a `citations` array, each entry carrying `fetch_tool`/`fetch_identifier` (e.g. `{"source_type": "email_thread", "source_id": 67695, "fetch_tool": "get_email", "fetch_identifier": 67695}`) so a citation resolves straight into the right raw-tool call. Mirrors the per-`source_type` join logic already used by `wiki_ingestion/promote.py`/`tier_classifier.py` — `email_thread`/`call_transcript`/`asset`/`repo_doc` join on the raw table's `id`, `implementation_task` joins on `odoo_task_id` instead (that table gets fully refreshed every raw-ingestion sweep, so its serial `id` isn't stable — see `get_implementation_task`'s docstring), and legacy junk values (`ticket` — dead, no live table since 2026-08-10; `unresolved`/`wiki_page` — pre-existing non-raw citation types) resolve to `fetch_tool: null` rather than erroring. `search_wiki` also previously never returned the page `id` at all — added, so a hit can chain into `get_wiki_page` or a fresh citations lookup without a second title-based search.

**Gap 2 — nothing enforced wiki-first ordering, and the skill files actively contradicted each other on it.** One line said `search_wiki` was "a genuine shortcut... do not skip past it by reflex" (soft, advisory); a few paragraphs later the decision tree's "A person" branch routed straight to `search_emails` with no mention of `search_wiki` at all — and every other content-shaped branch had the same gap. All 4 content-bearing skill files (`eoxs-wiki-db-skill.md`, `-general.md`, `-hr.md`, `-intern.md` — byte-identical passages in each, `staging_qa` defers to `full` so needed no separate edit) rewritten so wiki-first is a hard rule: `search_wiki`/`get_wiki_page` before any raw `search_*`/`list_*` call, every time, including for queries that sound raw-source-shaped ("find the email about X") — raw tools are the fallback for when the wiki has nothing relevant, not the first move.

**Also fixed alongside, since they compound the same failure mode when the wiki genuinely has no page yet**: `search_emails` was a single strict-AND `plainto_tsquery` against message bodies only (subject never matched at all, just selected for display), with no `ORDER BY`/`ts_rank` under a `LIMIT 20` — a real match could be silently dropped past the cutoff by arbitrary scan order even when found. Now two-tier: ranked AND-match against body-or-subject first, falling back to a ranked OR-match (`websearch_to_tsquery` over the query terms joined with `" or "`) only if the AND tier returns nothing — so a multi-entity query degrades to its best partial match instead of returning `[]`. `list_emails` gained a `date` parameter (`'YYYY-MM-DD'`, taking precedence over `month` when both are given) — previously only month-granularity filtering existed, so a query for a specific day had no precise tool-level answer.

One-off import: `loaders/import_employees_from_xlsx.py` merges EOXS's multi-sheet HR spreadsheet into one canonical row per person (deduped by name, then by shared email — catches same-person/different-spelling cases like "Dhrup" vs "Dhrup Kumar"). Deliberately excludes LinkedIn URLs, personal phone numbers, and — most importantly — a plaintext-password column present in the source sheet, never imported regardless of how this table gets used later.

One more `Server` instance (`server.py`'s module-level `server = build_server(FULL_CLEARANCE, enable_employee_tools=True, identity_name="full", asset_write_scope="all")`) exists purely for local stdio transport — always full clearance and live database, on the reasoning that anyone able to run this file locally already has raw `.env` Postgres credentials anyway. Not one of the five HTTP identities above — a separate, sixth `Server` object, for a different transport entirely.

**The redaction safety net** (`mcp_server/redaction.py`): runs on every tool call for every identity except `full`. Independent of the SQL-level tier filter — it inspects the actual text of what's about to be returned and strips anything matching a restricted category, catching cases where the original tier classification was wrong. Uses Sonnet (not Haiku), redacts by exact verbatim span removal (never regenerates text), fails closed on repeated API error. Every actual redaction is logged to `mcp_redaction_log` (schema 023) for follow-up.

**Transport**: SSE (`mcp.server.sse.SseServerTransport`), not Streamable-HTTP — a documented design choice, since an earlier Streamable-HTTP + Authorization-header attempt didn't fit what claude.ai's connector dialog exposes (only an OAuth Client Secret field, no generic header input). Routes per identity: `GET /mcp/<secret>/sse` and `Mount /mcp/<secret>/messages/`. Adding a real Streamable-HTTP endpoint (for non-claude.ai clients, e.g. a custom frontend) is planned but not yet built — see the DigitalOcean migration roadmap.

**Port**: `MCP_HTTP_PORT` (default **8091**), binds `127.0.0.1` only — reachable solely via the nginx `/mcp/` proxy.

**Tool count: 23** tiered/read-only tools, present for every identity:
`get_index`, `get_wiki_page`, `search_wiki`, `list_emails`, `search_emails`, `get_email`, `get_attachment_text`, `list_calls`, `search_calls`, `get_call`, `list_assets`, `search_assets`, `get_asset`, `list_clients`, `list_contacts`, `get_client_profile`, `get_client_file`, `list_implementation_tasks`, `search_implementation_tasks`, `get_implementation_task`, `list_repo_docs`, `search_repo_docs`, `get_repo_doc`.
(`list_assets`/`search_assets`/`get_asset` added 2026-08-12 alongside the new `assets` table — see `docs/raw-ingestion.md` §2 Assets. `get_asset` returns the full raw document; the corresponding wiki page under `wiki/sources/assets/` is a synthesized summary, not a substitute for the original text. `list_repo_docs`/`search_repo_docs`/`get_repo_doc` added 2026-08-26 alongside the new `repo_docs` table, §5.5 below — every row is hardcoded `tier1`, so these three tools are present on every identity's tool list but only ever return non-empty results for `full`/`ayan` in practice; `hr`/`general`/`intern`/`staging_qa` get an empty list/error just like calling any other tier1-only tool with insufficient clearance, not a separate code path.)
Plus **7 employee-directory tools** (§5.1) and **1–2 asset-write tools** (§5.2), present only for `full`/`ayan`/`hr`/`staging_qa` — **32 tools total** for `full` (23 + 7 + 2), **32** for `ayan` (identical tool set to `full`, distinguished only by its `changed_by` attribution, see §5's identity table), **31** for `hr` (23 + 7 + 1, no `create_asset`), still **23** for `general`/`intern`. `staging_qa` (§5.3) also sees **32** (unrestricted, like `full` — same 23 read + 7 employee + 2 asset-write tool set) but every one of them targets `eoxs_wiki_staging`, not live.

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

## 7. Total code size (current, `wc -l`, recomputed 2026-09-07)

| Directory | Lines |
|---|---|
| `ingestion/` | 6,479 (36 files) |
| `mcp_server/` | 2,155 (includes `redaction.py`, `employees.py`, `asset_writes.py`, `repo_docs.py`) |
| `wiki_ingestion/` | 4,019 (22 files) |
| `loaders/` | 1,564 |
| `parsers/` | 365 |
| **Total (Python)** | **14,582** |
| `schema/` (SQL, not counted above) | 1,159 lines across 36 numbered migrations |

Treat this table as a point-in-time snapshot, not a maintained fact — see `docs/raw-ingestion.md` §1 and `docs/wiki-ingestion.md` §1 for the per-directory file lists (both already note they go stale between updates; re-run `wc -l` rather than trusting an old number here).

## 8. Environment variables (names only — never commit or share actual values)

Since 2026-09-04 (§2) these are split across per-service `.env.<service>` files rather than one shared `.env` — the groupings below double as which file actually carries which variable; see §2's table for the unit→file mapping. `.env` itself (no suffix) still exists for local/manual script use.

**Ingestion server / webhooks:** `INGESTION_SERVER_PORT`, `INGESTION_WEBHOOK_SECRET`, `FIREFLIES_WEBHOOK_SECRET`, `FATHOM_WEBHOOK_SECRET`

**Postgres:** `PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER`, `PGPASSWORD`. `PGDATABASE_STAGING` is **not actually set** in this droplet's `.env` (confirmed 2026-08-25) despite appearing in earlier versions of this list — `mcp_server/db.py`'s `get_staging_conn()` reads it via `os.environ.get("PGDATABASE_STAGING", "eoxs_wiki_staging")`, so the hardcoded fallback silently does the real work. No bug results (the physical `eoxs_wiki_staging` database is reached correctly either way), but don't go looking for this variable in `.env` — it isn't there, and doesn't need to be.

**MCP server:** `MCP_URL_SECRET`, `MCP_AYAN_URL_SECRET` (added 2026-08-24 — see §5's `ayan` identity note; this one is easy to miss since it landed inside an unrelated commit), `MCP_HR_URL_SECRET`, `MCP_GENERAL_URL_SECRET`, `MCP_INTERN_URL_SECRET`, `MCP_STAGING_URL_SECRET`, `MCP_HTTP_PORT`

**Anthropic:** `ANTHROPIC_API_KEY` (spam/relevance filters), `CLASSIFIER_ANTHROPIC_API_KEY` (tier classification — deliberately separate for independent cost tracking)

**Gmail**: two registered OAuth clients now, not one triplet per account — `GMAIL_OAUTH_CLIENT_ID`/`GMAIL_OAUTH_CLIENT_SECRET` (the legacy Desktop-app client `raj_gmail`/`ron_gmail`/`remya_gmail` originally used) and `GMAIL_OAUTH_WEB_CLIENT_ID`/`GMAIL_OAUTH_WEB_CLIENT_SECRET` (the Web-app client every self-serve-connected account, including `ron_gmail` after it was reconnected, refreshes against). Legacy per-account triplets (`RAJ_/RON_/REMYA_GMAIL_CLIENT_ID/SECRET/REFRESH_TOKEN`) still exist in `.env.ingestion` but are superseded — actual refresh tokens live in the `oauth_accounts` table (`docs/raw-ingestion.md` §2 Gmail), not read from these env vars anymore. Also `OAUTH_REDIRECT_BASE_URL` (shared with Zoho's callback).

**Zoho**: same shape — `ZOHO_CLIENT_ID`/`ZOHO_CLIENT_SECRET` (legacy client, `support_zoho`) and `ZOHO_WEB_CLIENT_ID`/`ZOHO_WEB_CLIENT_SECRET` (self-serve-connected accounts). `ZOHO_REFRESH_TOKEN` is likewise superseded by the DB-backed `oauth_accounts` row.

**Fireflies / Fathom:** `FIREFLIES_API_KEY`, `RON_FATHOM_API_KEY`

**Odoo** (one pair per tenant): `GREER_ODOO_USERNAME/PASSWORD`, `ESS_ODOO_USERNAME/PASSWORD`, `DPS_ODOO_USERNAME/PASSWORD`, `PPC_ODOO_USERNAME/PASSWORD`, `THREEGM_ODOO_USERNAME/PASSWORD`, `SABRE_ODOO_USERNAME/PASSWORD`, plus `EOXS_TICKETS_ODOO_USERNAME/PASSWORD` (the central tickets+invoices instance)

**Linear:** `LINEAR_EDB_API_KEY`, `LINEAR_EDB_TEAM_KEY` (the ones actually read by `linear_report.py` — see the note in `docs/linear-integration.md` about two *stale, unused* look-alike names, `LINEAR_API_KEY`/`LINEAR_TEAM_KEY`, that also exist in `.env` but aren't what the code reads)

**pgweb:** `PGWEB_DB_PASSWORD`, `PGWEB_HTTP_PASSWORD`

**wiki_ingestion internals:** `WIKI_CYCLE_ID`, `WIKI_SOURCE_KIND` (set per sub-agent invocation, not meant to be set manually)

## 9. Where this fits in the overall system

The backend server is the one physical machine everything else in this document set lives on: it hosts the database (`docs/postgres-database.md`), runs the raw-ingestion fetchers and their schedule (`docs/raw-ingestion.md`), runs the wiki-synthesis pipeline (`docs/wiki-ingestion.md`), and is where the Linear-reporting code executes from (`docs/linear-integration.md`). It also hosts the sibling `eoxs-frontend-threads` system (§2) and a Claude Code CLI / Codex CLI environment used directly for admin and development work (including a persistent `claude --teleport` session) — a real, load-bearing use of this being a full VPS, not just a place to run services, and the main reason a migration to a pure container-platform (see the DigitalOcean migration roadmap) can't simply move everything off it. Nothing in this system runs anywhere else — there's no separate worker fleet, no managed cloud database, no serverless functions. One VPS, nine `eoxs-wiki-db` services (the seven listed in §2 plus `eoxs-healthcheck.service`/`.timer`, §2.1) plus one sibling-repo service, one Postgres instance.
