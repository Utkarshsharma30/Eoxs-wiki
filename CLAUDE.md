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

## Current state, as of 2026-08-12 — the things most likely to matter immediately

- **4 MCP identities**, not 3: `full` (Raj, everything), `hr` (confidential + general,
  payroll visible, every other dollar amount stripped), `general` (confidential + general,
  *every* dollar amount stripped including payroll, plus employee-activity/performance data
  stripped), `intern` (general only, every dollar amount stripped). See
  `docs/backend-server.md` §5 for exact category names.
- **Tickets and invoices are gone from every MCP tool** (`get_ticket`/`search_tickets`/
  `get_invoice`/`search_invoices` removed 2026-08-10) — that data now lives only in the
  separate `eoxs-teams` Odoo connector. **17 tools remain.**
- **Gmail accounts are now DB-backed, not hardcoded `.env` triplets** (`oauth_accounts` table,
  2026-08-12) — `raj_gmail`/`ron_gmail`/`remya_gmail` migrated over unchanged (same behavior:
  remya excluded from the recurring sweep via `raw_sweep_enabled=false`, not a name check
  anymore). New accounts connect via a self-serve OAuth flow (`ingestion/oauth_gmail.py`,
  `python -m ingestion.oauth_gmail invite <label> <name>`) — the account owner clicks a
  one-time link, logs into Google directly (never sees our system, never types a password
  anywhere we control), and is picked up by the next 2-hour sweep automatically, no `.env`
  edit or restart needed. See `docs/raw-ingestion.md` §2 Gmail.
- **The same self-serve OAuth pattern now covers Zoho too** (`ingestion/oauth_zoho.py`,
  2026-08-12) — `support_zoho` migrated into `oauth_accounts` unchanged
  (`client_type='legacy'`). New Zoho accounts connect the same one-link way Gmail's do, with
  one difference: Zoho requires a per-mailbox `external_account_id`, auto-discovered via
  `GET /api/accounts` right after the token exchange — nothing manual. Also: `email_threads.source_account`
  was a fixed Postgres enum (only the 4 original accounts) — converted to plain `TEXT`, since a
  rigid enum meant every future self-serve-connected account needed its own schema migration
  before it could write a single row, defeating the point of self-serve. See
  `docs/raw-ingestion.md` §2 Zoho.
- **Known, unresolved bug**: the recurring sweep still writes new rows to `tickets` despite
  the removal above — the fix never reached `tickets_fetcher.py`'s registration in the sweep
  itself. See `docs/raw-ingestion.md` §12. Real, live, growing — not just a doc gap.
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
