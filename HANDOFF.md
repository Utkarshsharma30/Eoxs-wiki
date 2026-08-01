# eoxs-wiki-db — Full Handoff Context for VPS-Native Claude

This document is written to be pasted into a fresh Claude session running natively on the
Ubuntu VPS (`deploy@5.223.44.95`, tmux session `eoxs-wiki-db`, project at
`~/eoxs-wiki-db/`), so that session can continue this build with zero loss of context.
Read this fully before touching anything. It describes real, live, already-working
infrastructure — not a plan.

---

## 1. The product vision (why this exists)

Raj (the user) runs EOXS, and has a personal "second brain" Obsidian vault
(`raj-wiki-vault`, GitHub repo `eoxssecondbrain/raj-wiki-vault`, branch `data`) that
contains: raw source data (`raw/` — emails, support tickets, invoices, call transcripts,
per-client implementation logs) and LLM-synthesized wiki pages (`wiki/` — entity/concept/
source/analysis/overview/prospect pages, cross-linked via `[[wikilinks]]`). That vault has
two live automations already running in production:

1. **Render pipeline** (`raw-vault-pipeline-server` + `raj-vault-mcp-server`, both on
   Render.com, code in `tools/` of the vault repo) — fetches new emails/tickets/calls/
   invoices from Gmail/Zoho/Fireflies/Fathom/Odoo APIs and commits new `.md` files into
   `raw/` on the `data` branch. Webhook-triggered (Gmail Pub/Sub, Fireflies, Fathom Svix)
   plus a daily cron fallback. This has been running reliably for a long time.

2. **wiki-agent** (Ubuntu VPS, SAME VPS as this project but a DIFFERENT tmux session —
   see section 3) — an autonomous Claude/Codex pipeline that watches for new `raw/`
   commits and writes `wiki/` pages, using Linear (team `WIK`) as its audit trail. Runs
   every 1 minute (ingest) + every 6 hours (Codex↔Claude adversarial review sweep). Fully
   documented in a separate architecture doc the user has (not reproduced here in full,
   but the relevant parts are summarized in section 3).

**The strategic goal, in the user's own words**: "The point of shifting to a database is
that when a client in future buys Poder (this system of having complete data of a company
and making an LLM out of it), we can cleanly store all their data in a database... We do
not want to do anything with repos moving forward... backend of this product [should not
be] repos maintained locally." "Poder" is the future productized version of this entire
system, sold to other companies, not just EOXS. **Postgres is meant to become the actual
backend architecture for that product — not a side project, not an addition bolted onto
the existing repo-based system.** The repo-based system (`raj-wiki-vault`) stays running
**exactly as-is, permanently, in parallel** — nothing about it should ever be touched by
this work. This new system is fully separate and additive.

Given that, the practical build has two halves:
- **Raw ingestion → Postgres directly** (in progress, this document's main focus)
- **Wiki ingestion → Postgres directly**, replacing wiki-agent's git-worktree mechanics
  with Postgres transactions (NOT STARTED YET — see section 8)

---

## 2. Where things physically live

- **Local reference implementation** (Windows machine, `C:\Users\PV\Desktop\eoxs-wiki-db\`):
  the FIRST version of this system, built and fully validated against a real local Postgres
  loaded with the ENTIRE vault's historical data (30,183 email threads, 1,041 wiki pages,
  1,923 tickets, 185 sales orders, 2,366 calls — all loaded from the vault's `raw/`/`wiki/`
  files via one-time batch loaders in `loaders/`). This also has a working local MCP server
  (`mcp_server/`) registered with Claude Code CLI, mirroring the vault's existing OV2 MCP
  server's tool set, side-by-side-tested and confirmed reliable. **This local version is a
  reference/dev environment — the schema in it is authoritative and proven, but it is NOT
  the live system.**

- **This VPS project** (`~/eoxs-wiki-db/`, git repo `https://github.com/eoxssecondbrain/
  eoxs-wiki-db.git`, branch `main`): the REAL, live, 24/7 system. Same schema as the local
  reference (ported via git — same repo, pushed from Windows, cloned here). This is where
  raw-ingestion (Gmail/Zoho done, Fireflies/Fathom/Odoo not yet) is being built. **This is
  what you should be extending.**

- **`raj-wiki-vault` repo clone(s) on this VPS** (`~/raj-wiki-vault`, `~/raj-wiki-vault-main`)
  and the **`wiki-agent` project** (`~/wiki-agent/`) — these belong to the OLD, git-based
  system (Render pipeline + wiki-agent). **Do not touch, read from only if explicitly
  instructed, never write to.** They run in a SEPARATE tmux session from this one.

---

## 3. Critical: two separate tmux sessions on the same VPS

There are (at least) two tmux sessions on this box:
1. The **wiki-agent session** — runs the existing git-based wiki-agent automation
   (1-minute ingest cron + 6-hour review sweep), reads/writes `~/raj-wiki-vault`. This is
   LIVE and RUNNING. Never interact with it, never run commands there, never assume its
   state.
2. **THIS session** (`eoxs-wiki-db`) — where you are now, working in `~/eoxs-wiki-db/`.
   This is the DB-native system. Everything in this handoff concerns this session only.

The user deliberately separated these to avoid any risk of the new work interfering with
the working automation. Preserve that separation absolutely.

---

## 4. Postgres — live setup, already working

- PostgreSQL 16.14, installed via the official PGDG apt repo (not Ubuntu's default repo),
  running as `postgresql@16-main` systemd service.
- Two databases: **`eoxs_wiki`** (LIVE) and **`eoxs_wiki_staging`** (TEST). Both owned by
  role `eoxs_app` (non-superuser, password auth, `Eoxs6333_kronox_db` — yes this is in
  plaintext in `.env` and was pasted in chat once already; the user said this is fine for
  now but it's the weakest credential in the system).
- Both databases have the IDENTICAL schema (14 real tables + `schema_migrations` bookkeeping
  table), applied via `schema/run_migrations.py` (idempotent, tracks applied files in a
  `schema_migrations` table, safe to rerun). Currently at migration `012_
  nullable_source_file_path.sql` (see section 5 for the full migration list and why each
  one exists).
- Connection info lives in `~/eoxs-wiki-db/.env` (gitignored, never committed — verified
  multiple times with `git check-ignore -v .env`). To apply a migration to staging instead
  of live: `PGDATABASE=eoxs_wiki_staging python schema/run_migrations.py` (env var override
  beats the `.env` default, since `load_dotenv()` never overwrites an already-set env var).

**Design decision, confirmed with the user**: raw data (emails/tickets/invoices/calls) is
written to BOTH `eoxs_wiki` and `eoxs_wiki_staging` directly, no gate, no review — because
it's deterministic parsing/API data, no LLM judgment involved, low risk. Only WIKI
ingestion (the LLM-synthesized content) will go through a staging-first, dual-gate,
promote-on-confidence workflow (structural/referential checks + the existing Codex↔Claude
review-sweep logic) before landing in live. **This distinction is important: don't add
gating to raw ingestion, and don't skip it when the wiki-ingestion rewrite happens later.**

---

## 5. Schema — full migration history and rationale

All in `schema/*.sql`, applied via `schema/run_migrations.py`. In order:

1. `001_extensions.sql` — `pg_trgm` for fuzzy title matching.
2. `002_reference.sql` — `clients`, `contacts`. Seeded from the vault repo's
   `tools/contact_registry.yaml` (8 clients: sabre-alloys, 3gm-steel, eastern-states-steel,
   discount-pipe-steel, ppc-metals, greer-steel, rw-conklin-steel, brannon-steel) +
   `tools/config.yaml`'s `odoo_implementation.clients` block (6 of those 8 have live Odoo
   instances: greer, ess, dps, ppc, 3gm, sabre — the other 2 don't have Odoo implementation
   tracking).
3. `003_emails.sql` — `email_threads` → `email_messages` → `email_attachments`. Unified
   schema across raj_gmail/ron_gmail/remya_gmail/support_zoho (an enum `email_source_
   account`). Natural key: `(source_account, gmail_thread_id)` UNIQUE. `is_quarantined`
   boolean instead of a separate spam table (used by the file-based loader for
   `raw/_spam_quarantine/`, not relevant to the new API-direct fetchers, which just skip
   writing spam entirely rather than writing-and-flagging).
4. `004_wiki.sql` — `wiki_pages`, `wiki_links` (the `[[wikilink]]` graph as real rows,
   `to_page_id` nullable for unresolved/future links — deliberately NOT dropped, matches
   the vault's own "a cross-reference that doesn't exist yet is a placeholder, not an
   error" convention), `wiki_citations` (polymorphic link from a wiki page to whatever raw
   source it cites), `wiki_flags` (extracted `⚠️ CONTRADICTION` / `🔍 UNVERIFIED` callouts).
5. `005_operational.sql` — `ingest_log`, `db_sync_state` (mtime-based incremental-skip
   tracking for the FILE-BASED loaders only — not used by the new API-direct ingestion,
   which uses `sync_cursors` instead, see migration 011).
6. `006_wiki_title_not_unique.sql` — relaxed `wiki_pages.title` from UNIQUE to a plain
   index, because the real vault has genuine pre-existing duplicate titles at different
   paths (e.g. two separate files both titled "Jamie Hansen.md") — an ambiguity that
   already exists in Obsidian's own wikilink resolution, not something to paper over.
7. `007_wiki_updated_raw.sql` — added `wiki_pages.updated_raw` TEXT column. Some vault
   pages' `updated:` frontmatter is a rich annotation like `"2026-07-30 (catch-up sync —
   long narrative...)"`, not a bare date. `updated_date` keeps just the leading date;
   `updated_raw` preserves the full original string so nothing is lost.
8. `008_tickets.sql` — `tickets` → `ticket_events` → `ticket_attachments`. Frontmatter is
   authoritative (body's recap table can drift, per the vault's own Odoo-export quirks).
9. `009_invoices.sql` — `sales_orders` → `order_lines`.
10. `010_calls.sql` — `call_transcripts` (Fireflies + Fathom UNIFIED via enum
    `call_source`, NOT separate tables — `source` column is always explicitly queryable, so
    "give me the latest Fathom calls" is just `WHERE source='fathom'`) → `call_segments`
    (per-speaker transcript turns).
11. `011_ingestion_state.sql` — **added specifically for the new DB-native ingestion
    service** (this is what you're extending): `sync_cursors` (source TEXT PRIMARY KEY,
    last_synced_at) — one row per source (`raj_gmail`, `ron_gmail`, `remya_gmail`,
    `support_zoho`, `fireflies`, `fathom`, and will need one per Odoo client e.g.
    `odoo_greer`, `odoo_ess`, etc. once that fetcher is built); `message_ids_seen`
    (message_id PK, thread_source_account, thread_id) — cross-thread dedup, mirrors the old
    Render pipeline's SQLite `message_ids` table.
12. `012_nullable_source_file_path.sql` — **critical for API-direct fetchers**: relaxed
    `source_file_path` from `NOT NULL UNIQUE`/`NOT NULL` to just nullable on
    `email_threads`, `tickets`, `sales_orders`, `call_transcripts` (NOT `wiki_pages` —
    that one still uses `source_file_path` as its real natural key, pending the wiki-
    ingestion redesign). Reason: API-fetched rows have no filesystem path at all; the real
    natural key for these tables is a SEPARATE unique constraint that already existed
    (`email_threads`: `source_account+gmail_thread_id`; `tickets`: `ticket_number`;
    `sales_orders`: `order_number`; `call_transcripts`: `source+external_id+
    source_file_path` — note this last one may need revisiting since `source_file_path`
    is now nullable and is PART of that composite unique key, meaning two NULL-path rows
    with the same source+external_id could theoretically collide oddly with Postgres's
    NULL-handling in unique constraints; NOT YET HIT IN PRACTICE but worth being aware of
    if call_transcripts writes ever show duplicate-row bugs).

**If you need a new migration**: create `schema/0XX_description.sql`, then run
`python schema/run_migrations.py` (applies to whatever `PGDATABASE` is set to — run it
twice, once for each database, or write a tiny wrapper that does both). The migration
runner is idempotent and tracks what's applied — never edit an already-applied migration
file; always add a new one.

---

## 6. Ingestion service — architecture, proven patterns, and what's built

### Directory: `ingestion/`

- **`db.py`** — `get_live_conn()`, `get_staging_conn()`, and the CENTRAL pattern:
  `dual_write(write_fn, *args, **kwargs)`. Calls `write_fn(conn, *args, **kwargs)` against
  LIVE first (must succeed — exception propagates, aborts the whole item), THEN against
  STAGING as best-effort (exception caught, logged as a warning, swallowed — staging being
  down must never block live data or advance/stall the cursor). `write_fn` must commit
  internally and return a value (only live's return value is used by callers).

- **`state.py`** — cursor/dedup helpers backed by LIVE only (staging never drives cursor
  decisions): `get_last_synced_at(source)`, `set_last_synced_at(source, when)`,
  `sync_since(source, safety_overlap_days=2)` (returns `last_synced_at - overlap`, mirrors
  the old pipeline's clock-skew/late-webhook tolerance), `is_message_seen(message_id)`,
  `mark_messages_seen(message_ids, source_account, thread_id=None)`, `now_utc()`.

- **`retry.py`** — `call_with_retry(fn, max_retries=8, base_delay=5, max_delay=120,
  is_retryable=None, retry_after_getter=None)`. Generic exponential backoff with optional
  Retry-After header honoring. **NOT YET WIRED INTO gmail_fetcher.py or zoho_fetcher.py**
  — those currently rely on the Google API client's own built-in `num_retries=8` retry
  (Gmail) and raw `httpx` calls with no explicit retry loop yet (Zoho — has worked fine in
  testing so far, but has no protection against transient failures). **Fireflies and
  Fathom's real APIs DO need explicit retry/backoff** (per the original Render pipeline:
  Fireflies handles both HTTP 429 and GraphQL-body-level `too_many_requests` errors, with
  Retry-After-aware backoff up to 8 retries + a 0.3s pause between detail fetches; Fathom
  uses Retry-After-or-15*2^n-capped-240s backoff, 5 retries, plus a mandatory 2.0s pause
  between LIST pages). **Use `retry.py`'s `call_with_retry` for those — this is exactly
  what it was built for and hasn't been used yet.**

- **`spam_filter.py`** — `is_eoxs_relevant(subject, body, client=None)`. Ported VERBATIM
  in behavior from the Render pipeline's `is_eoxs_relevant()`: Claude Haiku
  (`claude-haiku-4-5-20251001`), `max_tokens=5`, discards only marketing/newsletters/cold-
  outreach/automated-receipts/phishing, keeps everything else INCLUDING personal
  correspondence, defaults to KEEP on ambiguity or any API error (fail-open, never raises).
  **Gmail/Zoho ONLY — never call this for Fireflies/Fathom/Odoo, matching the original
  pipeline exactly** (those sources have no relevance filtering at all — Fireflies/Fathom
  fetch everything unconditionally).

- **`write_email.py`** — `write_thread(conn, *, source_account, gmail_thread_id, subject,
  from_addr, to_addr, message_count, participants, thread_dates, tags, is_quarantined,
  generated_at, messages, attachments_by_message_index=None)` and
  `existing_message_count(conn, source_account, gmail_thread_id)`. This is the SHARED
  write-layer for BOTH Gmail and Zoho (same `email_threads` schema, same natural key). Do
  NOT duplicate this logic for Zoho — Zoho's fetcher already correctly imports and reuses
  it. If you need a similar write-layer for calls (Fireflies/Fathom) or tickets (if a
  ticket-webhook ever gets added) or invoices, follow this EXACT pattern: takes a `conn`
  as first arg (so `dual_write` can call it twice), does its own `DELETE FROM
  child_table WHERE parent_id = %s` before re-inserting children (full replace, not merge,
  on every upsert — this matches the file-based loaders' behavior too), commits internally,
  returns the parent row's `id`.

- **`gmail_fetcher.py`** — **FULLY BUILT AND VALIDATED** against real production Gmail
  accounts (raj_gmail, ron_gmail, remya_gmail all tested with real OAuth, real API calls,
  real spam classification, real Postgres writes verified in both live and staging).
  Usage: `python -m ingestion.gmail_fetcher --account raj_gmail [--dry-run] [--limit N]
  [--no-classify]`. Key implementation notes:
  - Env vars: `{PREFIX}_CLIENT_ID`, `{PREFIX}_CLIENT_SECRET`, `{PREFIX}_REFRESH_TOKEN`
    where PREFIX is `RAJ_GMAIL`/`RON_GMAIL`/`REMYA_GMAIL` (i.e. the account dict values in
    `ACCOUNTS` already include `_GMAIL`, so DON'T add another `_GMAIL` in the f-string —
    this exact bug was hit and fixed once already: `f"{prefix}_GMAIL_REFRESH_TOKEN"` was
    wrong, `f"{prefix}_REFRESH_TOKEN"` is correct).
  - Refresh-token-only auth (no interactive flow), `Credentials(...).refresh(Request())`.
  - `num_retries=8` passed to every `.execute()` call (leverages googleapiclient's built-in
    backoff — this is DIFFERENT from and doesn't use `retry.py`).
  - Body decoding: prefers `text/plain`, walks multipart, falls back to crude HTML-tag
    stripping via regex if only HTML is available.
  - Dedup: checks `existing_message_count()` (skip if existing thread's message_count >=
    fresh count), THEN checks `is_message_seen()` for every message ID in the thread (skip
    if ALL are already seen — cross-thread dedup).
  - Spam-classified threads still get `mark_messages_seen()` called (so they're never
    reconsidered on future runs) even though nothing is written for them.
  - Cursor (`set_last_synced_at`) only advances on a non-dry-run call, using the run's
    OWN start timestamp (`now_utc()` captured BEFORE the fetch loop), not "now" at the end
    — standard practice to avoid missing anything that arrived mid-run.

- **`zoho_fetcher.py`** — **FULLY BUILT AND VALIDATED** (real OAuth, real search/content
  fetches, real spam classification, real Postgres writes confirmed: 4 rows landed
  correctly in both databases, `support_zoho` account). Usage: `python -m
  ingestion.zoho_fetcher [--dry-run] [--limit N] [--no-classify]`. Key implementation
  notes:
  - **CRITICAL BUG ALREADY HIT AND FIXED**: every single Zoho Mail API endpoint MUST be
    scoped under `/accounts/{account_id}/` — e.g. the correct URL is
    `{MAIL_API_BASE}/accounts/{account_id}/messages/search`, NOT
    `{MAIL_API_BASE}/messages/search`. This was verified against the real, working
    `ZohoClient` class in the OLD pipeline's `tools/email_to_obsidian/
    email_to_obsidian.py` (lines ~200, 230, 259, 272, 284 all include this segment). If you
    build ANY new Zoho endpoint call (e.g. for attachment fetching, not yet ported), it
    WILL need this same `/accounts/{account_id}/` prefix — don't repeat this bug.
  - `ZOHO_ACCOUNT_ID = "5146160000000008002"` is HARDCODED in the file (not an env var) —
    it's not a secret, it's a fixed numeric identifier, matches `tools/config.yaml`'s
    `zoho.account_id` in the old pipeline. If a second Zoho account is ever added, this
    would need to become configurable, but for now there's only one.
  - Auth: OAuth2, access token cached in-memory on the `ZohoClient` instance, auto-refresh
    on 401 with a single retry (`_request(..., retried=False)` recursion guard).
  - Listing: unified `/messages/search?searchKey=date:` endpoint (an empty or omitted
    `searchKey` both 400 — `"date:"` is the smallest working unscoped query, per the old
    pipeline's live-tested finding — this spans every folder except Spam/Trash in one
    call). Paginated `start`/`limit=200`.
  - Date filtering is CLIENT-SIDE against `receivedTime` (Zoho's server-side
    `fromDate`/`toDate` search operators are documented as unreliable in the old pipeline's
    code comments — don't try to "optimize" this into a server-side filter without
    re-verifying that claim).
  - Content fetch is a SEPARATE per-message API call (`fetch_message_content(folder_id,
    message_id)`) — the search/list endpoint does NOT include message bodies.
  - Threads are grouped by `threadId` (fallback to `messageId` if a message has no
    `threadId` — rare but happens).
  - Reuses `write_email.py`'s `write_thread()` UNCHANGED — same `email_threads` schema,
    `source_account="support_zoho"`.

### Not yet built (see section 7 for exact next steps)

- `fireflies_fetcher.py` — GraphQL API, needs a NEW write-layer (`write_call.py` or
  similar, following the `write_email.py` pattern exactly) for `call_transcripts` +
  `call_segments`. NO spam filtering (fetches everything). HAS client routing logic in the
  old pipeline (`classify_participants()` in `tools/pipeline_common/contacts.py` — decides
  whether a call is tagged as belonging to a specific client vs. general/unmatched; in the
  DB world this should set `call_transcripts.client_id` rather than choosing a folder path).
  Retry: HTTP 429 + GraphQL-body `too_many_requests`, Retry-After-aware, up to 8 retries,
  0.3s pause between detail fetches — USE `retry.py`'s `call_with_retry` for this, it
  hasn't been used anywhere yet but was built exactly for this kind of need.

- `fathom_fetcher.py` — REST API (`https://api.fathom.ai/external/v1`), cursor-paginated
  `/meetings` + `/recordings/{id}/transcript`. NO spam filtering, NO client routing (all
  Fathom calls are unmatched/general in the old pipeline too). Retry: Retry-After-or-
  15*2^n-capped-240s, 5 retries, MANDATORY 2.0s sleep between meeting-list pages (not just
  on error — this is a fixed rate-limit courtesy delay, not backoff). **Cursor**: the OLD
  pipeline's Fathom cursor was a workaround (borrowed the Fireflies SQLite table with a
  `fathom_` id prefix, because a dedicated cursor mechanism had a bug). In THIS system, do
  it properly: `sync_cursors` already has a row keyed by source name, just use
  `source="fathom"` directly — no workaround needed, migration 011 already supports this
  cleanly.

- `odoo_fetcher.py` — XML-RPC (not REST), 6 separate Odoo instances/databases (one per
  client: greer, ess, dps, ppc, 3gm, sabre — see `tools/config.yaml`'s
  `odoo_implementation.clients` block in the OLD vault repo for exact base_urls/db names/
  project_names per client, and the matching `{PREFIX}_ODOO_USERNAME`/`PASSWORD` env vars
  already transferred into THIS project's `.env`: `GREER_ODOO_*`, `ESS_ODOO_*`,
  `DPS_ODOO_*`, `PPC_ODOO_*`, `THREEGM_ODOO_*`, `SABRE_ODOO_*`). **Important behavioral
  note from the old pipeline**: this fetch is a FULL REFRESH every run (not incremental) —
  the old pipeline deletes and rewrites the entire output folder every time, specifically
  because Odoo-derived data is "entirely Odoo-derived and never hand-edited" so there's no
  human-edit-protection concern and no incremental-cursor complexity needed. In the DB
  world, this probably means: TRUNCATE or DELETE+reinsert all `implementation_tasks` rows
  for a given client on every run (note: there is NO `implementation_tasks` table in the
  CURRENT schema yet — this needs a new migration; the old file-based schema had client-
  specific Odoo Kanban task exports under `raw/clients/<slug>/implementation/` that were
  never ported into a Postgres table in this project). **This is the one source where you
  need to design new schema before writing the fetcher — check the local Windows reference
  implementation's `loaders/` directory first to see if `load_clients.py` or similar
  already has relevant patterns; if not, you'll need to design `implementation_tasks` (and
  possibly `implementation_task_events` for stage-change history, mirroring
  `ticket_events`) from scratch based on what the old pipeline's `tools/
  odoo_implementation_to_obsidian.py` writes.**

---

## 7. Immediate next steps, in order

1. **Add explicit retry/backoff to `zoho_fetcher.py` and `gmail_fetcher.py`** using
   `retry.py`'s `call_with_retry` — currently under-protected against transient failures
   compared to the old pipeline (Gmail has SOME protection via `num_retries=8` on the
   Google client; Zoho has NONE right now). Not urgent (both have worked in testing so
   far) but should happen before this runs unattended in production.
2. **Build `fireflies_fetcher.py`** — GraphQL, new `write_call.py` write-layer, wire in
   `retry.py`, wire in client routing (port `classify_participants()` logic — check if it's
   worth porting `tools/pipeline_common/contacts.py` wholesale as a new
   `ingestion/contacts.py`, reading `contact_registry.yaml`'s content, which you'd need to
   either copy into this repo or fetch some other way since this repo currently doesn't
   have a copy of `contact_registry.yaml` — CHECK FIRST whether this file needs to be
   transferred, similar to how `.env` secrets were transferred via `scp`, since it's not a
   secret but its content IS needed).
3. **Build `fathom_fetcher.py`** — reuses `write_call.py` from step 2, simpler than
   Fireflies (no client routing, no spam filter), but has its own REST auth/pagination.
4. **Design + build Odoo implementation schema + `odoo_fetcher.py`** — the one source
   needing new schema design, see section 6's notes above.
5. **Build the FastAPI ingestion server** (`ingestion/server.py` or similar) — webhook-
   triggered (Gmail Pub/Sub, Fireflies, Fathom Svix — same signature-verification schemes
   as the old pipeline's `tools/webhook_server.py`, which is a GOOD reference to read for
   the exact signature-checking code even though this new server won't reuse its file-
   writing logic) + a daily cron fallback (systemd timer or crontab, calling each fetcher
   once). Each webhook handler should just call the relevant fetcher module's main
   processing function (already built as importable functions, not just CLI scripts —
   e.g. `gmail_fetcher.process_account(account, ...)`, `zoho_fetcher.process_zoho(...)`).
6. **Linear integration for raw ingestion**: the user was explicit — **NOT** the same
   parent/child cycle structure as wiki-agent's `WIK` board. Just a lightweight
   confirmation signal: a NEW team (key `EDB`, already created — API key is in `.env` as
   `LINEAR_API_KEY`/`LINEAR_TEAM_KEY`), one issue per completed ingestion run with a short
   summary like "Raw ingestion run — 2026-08-01 — 47 new emails, 3 tickets, 0 errors". No
   workflow states beyond maybe Done/Failed, no parent/child hierarchy. This should
   probably be called from the FastAPI server after each run (or after each cron-
   triggered full sweep across all sources), not from inside each individual fetcher.

---

## 8. NOT YET STARTED: Wiki-ingestion DB-native rewrite

This is a SEPARATE, LARGER piece of work, explicitly sequenced AFTER raw-ingestion is
fully built and validated (per the user's own stated preference: "Build DB-native raw
ingestion FIRST, wiki-ingestion rewrite second"). Do not start this until raw-ingestion
(all 5 sources + the FastAPI server) is complete and the user has confirmed it's stable.

**The design, as discussed and agreed with the user (not yet built)**:
- Replace wiki-agent's git worktrees/branches/merges with Postgres transactions and a
  separate `eoxs_wiki_staging` schema-level write pattern (or possibly a literal second
  Postgres schema within the same staging database — this detail wasn't fully finalized).
- Each category-batch sub-agent (same "emails/raj_gmail", "tickets", etc. batching concept
  as the existing wiki-agent) runs against `eoxs_wiki_staging`, writes proposed pages there.
- Both a structural/referential gate (row-count sanity, no orphaned `wiki_links`/
  `wiki_citations`, no broken FKs — cheap, mechanical, zero LLM cost) AND the EXISTING
  Codex↔Claude review-sweep logic (ported to diff Postgres state instead of git diffs) must
  both pass before an atomic promotion of changed rows from staging to live.
- If either gate fails: leave staging as-is for inspection (don't roll back), flag it
  somehow (Linear, TBD exact mechanism — the user wants SOME Linear presence for wiki-
  ingestion, closer to matching wiki-agent's existing `WIK` board structure, unlike raw-
  ingestion's lightweight-only approach — this needs to be revisited/confirmed with the
  user when this phase starts, don't assume).
- Runs once daily (end of day), with the review/gate pass 15 minutes later — explicit
  user-specified schedule.
- The existing `WIK` Linear board / wiki-agent's Linear structure should be preserved and
  reused conceptually for THIS system's wiki-ingestion (unlike raw-ingestion which gets its
  own separate lightweight `EDB` board) — but the exact mechanics (same board? new board
  with the same STRUCTURE? ) were not fully pinned down in conversation. **Ask the user to
  clarify this specifically when starting section 8's work — do not assume.**

---

## 9. Things that have already gone wrong once — don't repeat them

1. **Env var name mismatches between code and `.env`** — caused multiple real failures
   (`RAJ_GMAIL_GMAIL_REFRESH_TOKEN` double-prefix bug, apparent "missing" vars that were
   actually just terminal-scrollback truncation hiding real content). **Before assuming a
   credential is missing, use `ingestion/_check_env.py`** (already built) — it prints
   ONLY variable names (never values) and definitively confirms presence/absence without
   relying on scrolling through terminal output. Run: `python ingestion/_check_env.py`.
2. **Zoho API URLs need `/accounts/{account_id}/`** — see section 6, already fixed but
   easy to reintroduce if extending Zoho functionality.
3. **Terminal scrollback is NOT a reliable way to verify command output** — screenshots/
   pastes from a small tmux pane routinely appeared to be missing data that was actually
   present, just scrolled off-screen. Prefer scripts that print a definitive final
   answer (like `_check_env.py`'s "ALL REQUIRED VARS PRESENT" line) over asking a human to
   visually scan a long list.
4. **`.env` is correctly gitignored** — verified multiple times with `git check-ignore -v
   .env` and `git status --short`. Keep it that way. Never `git add .env`, never commit
   secrets. If you add NEW secrets to `.env` in the future, this same discipline applies.
5. **Dry-run before write, always, for anything touching real external APIs or real
   production data** — every fetcher supports `--dry-run` specifically so this pattern can
   continue. Don't skip this step when building new fetchers, even though it feels slow.

---

## 10. How to verify your own work (the pattern used throughout this build)

For every new fetcher:
1. Write the code, checking field names/URL shapes against the OLD pipeline's real,
   working implementation in the vault repo's `tools/` directory whenever possible (it's
   the ground truth for "what actually works against this API") — don't guess API shapes
   from documentation alone if working reference code exists.
2. Syntax-check + import-check locally if possible (or on the VPS directly, since you're
   running natively here) before running against real APIs.
3. Run with `--dry-run --limit 5` (small limit) first — confirms auth works, fetch works,
   dedup/classification logic runs, and logs what WOULD be written, without touching
   Postgres.
4. Run again WITHOUT `--dry-run`, same small limit — writes for real.
5. Verify with a direct SQL query against `eoxs_wiki` AND `eoxs_wiki_staging` (e.g.
   `SELECT source_account, count(*) FROM email_threads GROUP BY source_account;`) that the
   expected rows landed in BOTH databases with matching data.
6. Only then consider that source "done" and move to the next one.

---

## 11. Summary of what's proven and working RIGHT NOW

- PostgreSQL 16.14 live+staging, identical schema, both verified.
- `eoxs-wiki-db` GitHub repo, private, cloned on VPS, push/pull working cleanly.
- All credentials (Gmail×3, Zoho, Fireflies key, Fathom key, Odoo×6, Anthropic, Linear)
  transferred and verified present via `ingestion/_check_env.py`.
- Gmail fetcher: fully working for all 3 accounts (raj/ron/remya), real OAuth, real spam
  classification, real dedup, real dual-write, verified in Postgres directly.
- Zoho fetcher: fully working, real OAuth, real search+content fetch, real spam
  classification, real dual-write, verified in Postgres directly.
- Current row counts as of this handoff (will keep growing as ingestion continues):
  `email_threads` — raj_gmail: 2, ron_gmail: 4, remya_gmail: 1, support_zoho: 4 (all from
  small `--limit 5` test runs, not full backfills — there is much more real data available
  once these run at full scale / on a schedule).

Everything else described above (Fireflies, Fathom, Odoo, the FastAPI server, Linear
integration for raw-ingestion, and the entire wiki-ingestion DB-native rewrite) is
DESIGNED AND AGREED but NOT YET BUILT. That's the actual remaining work.
