# Orientation — read this first

This is Cruz (`eoxs-wiki-db`), EOXS's internal second-brain: raw data ingestion →
AI-synthesized wiki → tiered MCP connectors. Everything in this file is meant to
get a fresh session (no prior conversation history) fully oriented before touching
anything. `HANDOFF.md` is superseded historical context — start here instead.

## Read next, in order

1. **`ARCHITECTURE.md`** — plain-language overview, no engineering background needed.
2. **`docs/backend-server.md`** — the server, every service, the MCP tier/redaction system.
3. **`docs/postgres-database.md`** — every table, real current row counts.
4. **`docs/raw-ingestion.md`** — every data source, and a known live bug (below).
5. **`docs/wiki-ingestion.md`** — how raw data becomes AI-written pages.
6. **`docs/linear-integration.md`** — status reporting to Linear.
7. **`docs/infrastructure-roadmap.md`** — where hosting is headed (DigitalOcean), what's
   decided, what's still open. Read this before proposing any infra change — the reasoning
   for a DO hybrid (not a pure PaaS move) took real back-and-forth to reach.
8. **`docs/local-dev-and-team-onboarding.md`** — local dev setup, team delegation model,
   and a troubleshooting section built from real mistakes made setting this up.
9. **`docs/training/`** — intern/new-hire orientation video scripts.

## Current state, as of 2026-08-14 — the things most likely to matter immediately

- **`_environment` field added to every write-tool result (2026-08-14)** — a real incident,
  not preventive-only: a `staging_qa` session with no skill file attached had zero factual
  basis to know it was in the sandbox (nothing in ANY tool result said so, only skill-file
  text did) and nearly asked the user to approve a write it wrongly believed was live.
  Nothing was actually written anywhere in that session — verified directly against both
  databases — but the belief was wrong. Now every write tool's result (all 4 employee +
  both asset tools, every identity, success or error alike) carries a server-asserted
  `_environment` string saying plainly which database it hit — stamped in `call_tool()`
  after redaction, never something a caller or a missing skill file can spoof or omit. See
  `docs/backend-server.md` §5.4.

- **4 MCP identities**, not 3: `full` (Raj, everything), `hr` (confidential + general,
  payroll visible, every other dollar amount stripped), `general` (confidential + general,
  *every* dollar amount stripped including payroll, plus employee-activity/performance data
  stripped), `intern` (general only, every dollar amount stripped). See
  `docs/backend-server.md` §5 for exact category names.
- **Tickets and invoices are gone from every MCP tool** (`get_ticket`/`search_tickets`/
  `get_invoice`/`search_invoices` removed 2026-08-10) — that data now lives only in the
  separate `eoxs-teams` Odoo connector. **20 read-only tools remain, present for every
  identity** (17 original + `list_assets`/`search_assets`/`get_asset`, added 2026-08-12
  alongside the new `assets` table — see `docs/raw-ingestion.md` §2 Assets).
- **Employee directory added 2026-08-12** (`employees` table + `mcp_server/employees.py`)
  — this server's first-ever write path. 7 tools (list/search/get + create/update/
  deactivate/reactivate_employee), gated to the `full` (Raj) and `hr` (Isha) identities
  only. Soft-delete only (`status` active/inactive, never a real `DELETE`); every write
  audited to `employee_change_log`. Deliberately outside the tiered-content system (no
  `access_tier` column) and outside the wiki-ingestion pipeline entirely — see
  `docs/backend-server.md` §5.1 and `docs/postgres-database.md`.
- **Asset writes added 2026-08-13** (`mcp_server/asset_writes.py`) — the second and, per
  explicit instruction, **last** write path this server will get; every other table stays
  read-only, permanently. `create_asset`/`update_asset` for the `assets` table (§ above):
  `full` gets both, any document; `hr` gets `update_asset` **only**, and only for
  `eoxs-salary-details` — any other slug is refused with a plain permission error.
  `create_asset` auto-classifies `access_tier` the same way the one-time import does
  (never a caller-supplied argument). `update_asset` does NOT reclassify `access_tier` on
  edit — it survives, matching every raw-ingestion writer's existing convention. Every
  write audited to `asset_change_log` (full old/new text, not just field diffs — see
  `docs/backend-server.md` §5.2 for why). Deliberately **wired INTO** wiki-ingestion,
  unlike employees: `wiki_ingestion/detect.py`'s existing `candidates_assets()` partition
  already watches `assets.updated_at`, so any create/update here is picked up and
  re-drafted into the wiki by the next scheduled 6-hour cycle automatically — no pipeline
  changes were needed. **Tool totals**: `full` 29 (20 read + 7 employee + 2 asset-write),
  `hr` 28 (20 + 7 + 1), still 20 for `general`/`intern`.
- **5th MCP identity added 2026-08-13: `staging_qa`** — a QA sandbox for testing the
  employee/asset write tools above without any risk to live data. Not a new write surface
  (still only `employees`/`assets`, nothing else) — the same two tool sets, unrestricted,
  but every single tool call this identity makes (read AND write) is transparently routed
  to the `eoxs_wiki_staging` **database** instead of live `eoxs_wiki`, via a new
  `mcp_server/db.py` ContextVar (`use_database()`) that `build_server()`'s `database` param
  sets for the duration of one tool call — required editing only `db.py` and one line in
  `call_tool()`, zero changes to any of the ~29 individual tool functions. A write through
  this identity structurally cannot reach the wiki-ingestion pipeline (confirmed by grep:
  `wiki_ingestion/` has zero references to `get_staging_conn`/`PGDATABASE_STAGING` — it only
  ever calls `get_live_conn()`). Every write is tagged `changed_by='staging_qa'` in
  `employee_change_log`/`asset_change_log` for easy identification;
  `loaders/reset_staging_qa_data.py --commit` wipes staging's `employees`/`assets`/both
  change-logs and re-mirrors `employees`/`assets` from live for a clean baseline between
  sessions. **Not documented in any of the 4 main skill files** — see the dedicated
  `deploy/eoxs-wiki-db-skill-staging-qa.md` instead. Full detail:
  `docs/backend-server.md` §5.3.
- **Do not confuse `eoxs_wiki_staging` (a separate physical database, used for raw-ingestion
  dual-write testing AND now `staging_qa`) with `wiki_staging` (a schema living inside LIVE
  `eoxs_wiki`, the draft-review workspace wiki pages sit in before promotion)** — two
  genuinely different things sharing a similar name; see `docs/postgres-database.md` §9.
  `wiki_ingestion/` only ever touches the latter.
- **Gmail accounts are now DB-backed, not hardcoded `.env` triplets** (`oauth_accounts` table,
  2026-08-12) — `raj_gmail`/`ron_gmail`/`remya_gmail` migrated over unchanged (same behavior:
  remya excluded from the recurring sweep via `raw_sweep_enabled=false`, not a name check
  anymore). New accounts connect via a self-serve OAuth flow (`ingestion/oauth_gmail.py`,
  `python -m ingestion.oauth_gmail invite <label> <name>`) — the account owner clicks a
  one-time link, logs into Google directly (never sees our system, never types a password
  anywhere we control), and is picked up by the next 2-hour sweep automatically, no `.env`
  edit or restart needed. See `docs/raw-ingestion.md` §2 Gmail.
- **The same self-serve OAuth pattern now covers Zoho too** (`ingestion/oauth_zoho.py`,
  2026-08-12) — `support_zoho` migrated into `oauth_accounts` unchanged (`client_type='legacy'`).
  New Zoho accounts connect the same one-link way Gmail's do, with one difference: Zoho requires
  a per-mailbox `external_account_id`, auto-discovered via `GET /api/accounts` right after the
  token exchange — nothing manual. Also: `email_threads.source_account` was a fixed Postgres
  enum (only the 4 original accounts) — converted to plain `TEXT`, since a rigid enum meant every
  future self-serve-connected account needed its own schema migration before it could write a
  single row, defeating the point of self-serve. See `docs/raw-ingestion.md` §2 Zoho.
- **New raw source category: `assets`** (curated internal reference docs — SOPs, company
  overview, ICP, salary register — 2026-08-12) — found while investigating a user report that
  this data "wasn't in the wiki": it *was*, wiki pages for all 15 were already here (migrated
  from `raj-wiki-vault` at some earlier point), but the raw layer backing them never existed,
  leaving their citations permanently unresolved. Backfilled via `ingestion/import_assets.py`
  (manual, re-runnable — not an ongoing fetcher, no external feed for hand-curated docs). See
  `docs/raw-ingestion.md` §2 Assets.
- **RESOLVED 2026-08-12** (was listed here as unresolved): the recurring sweep no longer writes
  new rows to `tickets`/`invoices` — `run_full_sweep()`'s source list in `ingestion/server.py`
  never had `tickets_fetcher.py`/`invoice_fetcher.py` removed from it when those tools were
  pulled from MCP on 2026-08-10, so it kept calling them every 2 hours regardless. Fixed; the
  17 stray rows this wrote (100% of what was in `tickets` — none of it reachable via any tool)
  were deleted. See `docs/raw-ingestion.md` §3 (Tickets & Invoices section).
- **`eoxs-frontend-threads` is a separate repo and separate service**
  (`github.com/eoxssecondbrain/eoxs-frontend-threads`), not part of this codebase — deployed
  alongside this system on the same box, own database (`eoxs_frontend_threads`), own venv.
  Its per-user-secret identity model is an open question — see
  `docs/infrastructure-roadmap.md`.
- **No backup exists for either database.** No uptime/health monitoring exists. Both have a
  fully specified plan in `docs/infrastructure-roadmap.md`, neither is built.
- **A DigitalOcean migration is planned but not started** — full phased roadmap in
  `docs/infrastructure-roadmap.md`. The key insight: this can't be a pure PaaS move, because
  the wiki-synthesis pipeline spawns `claude -p` as a subprocess and the box also hosts a
  persistent, interactively-used `claude --teleport` session — that category of work needs a
  real VPS (a Droplet), not a container platform. Only the stateless services (MCP
  connectors, webhook receiver, sweep) move to a PaaS layer.
- **No domain name yet** — everything is on the raw IP `5.223.44.95`, with a short-lived
  (~6-day) Let's Encrypt certificate. Real Streamable-HTTP transport support (for non-
  claude.ai clients, e.g. a custom frontend) is planned alongside getting a domain, not built.

## Standing practices for this repo

- **Commit and push after every unit of work**, without being asked — this has been an
  explicit, standing instruction across every session on this project. Don't let work sit
  uncommitted.
- **Verify against the live system, not just the code or docs, before concluding something
  is or isn't true.** This codebase has a real history of disk/deployed-state drift (see the
  git-stash incident referenced throughout the docs) — read the running process, query the
  live database, check `systemctl status`, don't assume a doc or a file on disk reflects
  what's actually deployed.
- **Never paste secrets (tokens, passwords, API keys) into chat.** If one ever ends up
  there anyway, treat it as burned and rotate it, regardless of whether anything went wrong.
- Every doc listed above is kept current, deliberately — when you change something the docs
  describe, update the doc in the same batch of work, not as a someday-cleanup task.
