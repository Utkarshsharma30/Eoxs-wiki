# Raw Data Ingestion

*Deep technical reference for how Cruz (`eoxs-wiki-db`) fetches raw data from 9 external sources, classifies it, and stores it — for developers being onboarded to ingestion work.*

## 1. The `ingestion/` directory

```
ingestion/
  server.py                   261  FastAPI app: webhooks + manual trigger + sweep entrypoint
  gmail_fetcher.py             321  Gmail (accounts are DB-backed, see oauth_gmail.py)
  oauth_gmail.py                 -  self-serve OAuth connect flow (mints invite links, exchanges tokens)
  zoho_fetcher.py               304  Zoho Mail (accounts are DB-backed, see oauth_zoho.py)
  oauth_zoho.py                   -  self-serve OAuth connect flow for Zoho (mirrors oauth_gmail.py)
  fireflies_fetcher.py          326  Fireflies (call transcripts)
  fathom_fetcher.py             297  Fathom (call recordings)
  odoo_fetcher.py                400  Per-client implementation Kanban boards (6 clients)
  tickets_fetcher.py             214  EOXS's own support tickets (central Odoo instance)
  invoice_fetcher.py             343  Sales orders + invoices (same central instance)
  write_email.py                101  email_threads/messages/attachments writer
  write_call.py                   84  call_transcripts/segments writer
  write_ticket.py                  77  tickets/events/attachments writer
  write_invoice.py                125  sales_orders + children writer
  write_implementation.py          128  implementation_tasks + children writer
  db.py                            65  dual_write() + connection helpers
  state.py                         83  sync_cursors + message_ids_seen helpers
  routing.py                       57  client_id resolution (email/domain matching)
  spam_filter.py                    50  email noise filter (Claude Haiku)
  call_relevance.py                 77  call noise filter (Claude Haiku)
  inline_tier_classifier.py         79  per-row access_tier classifier (Claude Haiku)
  tier_classifier.py               208  bulk backlog access_tier reclassifier
  ingest_log.py                    125  writes to the ingest_log audit table
  linear_report.py                  262  Linear GraphQL client + raw-sweep reporting
  retry.py                          38  shared retry helper
  _check_env.py                     40  env-var-presence diagnostic (has some stale names — see §12)
```
4,064 lines total, 23 files.

## 2. Each fetcher, in detail

Every fetcher shares the same overall shape: connect → figure out what's new since last time → fetch it (with pagination) → classify it (spam/relevance filter, then access-tier) → `dual_write()` it → advance the cursor.

### Gmail (`gmail_fetcher.py`) — accounts are DB-backed (`oauth_accounts`, since 2026-08-12)

- **API**: Gmail API v1, `users().threads().list()` + `.get(format="full")`.
- **Auth**: accounts live in the `oauth_accounts` table (schema/026_oauth_accounts.sql), not hardcoded
  `.env` triplets — `load_accounts(raw_sweep_only=False)` returns `{account_label: display_name}` for
  every `status='active'` row, `raw_sweep_only=True` additionally filters to `raw_sweep_enabled=true`
  (what the recurring sweep iterates). Only `refresh_token` is per-account and lives in the DB row.
- **Two OAuth clients, not one** (`oauth_accounts.client_type`, schema/027_oauth_client_type.sql):
  `raj_gmail`/`ron_gmail`/`remya_gmail` (`client_type='legacy_desktop'`) refresh against
  `GMAIL_OAUTH_CLIENT_ID`/`GMAIL_OAUTH_CLIENT_SECRET` — a Google "Desktop app"-type client, which
  cannot register a custom redirect_uri at all (Google restricts it to localhost/the deprecated OOB
  copy-paste flow). Every account connected through the self-serve flow below
  (`client_type='web'`) refreshes against `GMAIL_OAUTH_WEB_CLIENT_ID`/`GMAIL_OAUTH_WEB_CLIENT_SECRET`
  instead — a separate "Web application"-type client in the same Google Cloud project (same consent
  screen, same verification status), the only type Google allows a custom HTTPS redirect_uri on.
  `gmail_fetcher.py`'s `_load_account_credentials()` picks the right pair per account automatically;
  refreshing an already-issued token never re-validates redirect_uri, so this split is invisible to
  the 3 existing accounts.
- **Connecting a new account**: `ingestion/oauth_gmail.py` — `python -m ingestion.oauth_gmail invite
  <account_label> <display_name>` mints a single-use, expiring link (`oauth_connect_tokens` table).
  The account owner opens it, logs into Google directly (never sees our system, never types a
  password into anything we control), approves `gmail.readonly` access. Google redirects to
  `/oauth/gmail/callback` (mounted in `ingestion/server.py`), which exchanges the code for a refresh
  token server-side and upserts the `oauth_accounts` row — no `.env` edit, no service restart. The
  account is picked up by the very next sweep automatically. The redirect_uri is
  `https://5-223-44-95.nip.io/oauth/gmail/callback` (`OAUTH_REDIRECT_BASE_URL` in `.env`) —
  nip.io's wildcard DNS (`<ip>.nip.io` always resolves to `<ip>`, zero setup) stands in for a real
  domain, since Google rejects a raw IP literal as a Web-application client's redirect_uri ("must end
  with a public top-level domain"); see the `deploy/nginx-https.conf` server block for this hostname,
  with its own standard 90-day Let's Encrypt cert (separate from the ~6-day IP-address cert the bare-IP
  block uses). Swap this for a real subdomain once one exists (`docs/infrastructure-roadmap.md`).
  Current accounts: `raj_gmail`, `ron_gmail`
  (both `raw_sweep_enabled=true`), `remya_gmail` (`raw_sweep_enabled=false` — 2026-08-10, one-time
  historical pull only, not an ongoing source).
- **Entry point**: `process_account(account, *, dry_run, limit, safety_overlap_days, classify)`.
- **Incremental fetch**: `sync_since(account, safety_overlap_days)` → Gmail search query `after:YYYY/MM/DD`. Cursor advances via `set_last_synced_at()` on success.
- **Pagination**: pages of up to 100, following `nextPageToken` until exhausted or `limit` hit.
- Retries via `retry.py` (`GMAIL_NUM_RETRIES=8`). Attachments are metadata-only.

### Zoho (`zoho_fetcher.py`) — accounts are DB-backed (`oauth_accounts`, since 2026-08-12)

- **API**: Zoho Mail REST, `/api/accounts/{account_id}/messages/search`.
- **Auth**: same `oauth_accounts` pattern Gmail uses (`load_accounts()`, `client_type` picking
  `ZOHO_CLIENT_ID`/`SECRET` for `'legacy'` accounts vs `ZOHO_WEB_CLIENT_ID`/`SECRET` for `'web'`
  ones) — access token cached in-memory per `ZohoClient` instance, auto-refreshed on 401. Unlike
  Gmail, each account also carries `external_account_id` — Zoho's numeric per-mailbox identifier,
  required to build API URLs (`/accounts/{external_account_id}/...`); Gmail's API has no
  equivalent since it implicitly scopes to "me" via the token.
- **Connecting a new account**: `ingestion/oauth_zoho.py` — same one-click self-serve flow as
  Gmail's (`ingestion/oauth_gmail.py`), `python -m ingestion.oauth_zoho invite <account_label>
  <display_name>`. One difference: right after the token exchange, the callback auto-discovers
  `external_account_id` via `GET /api/accounts` (no library equivalent to
  `google-auth-oauthlib`, so this is hand-rolled with `httpx` — deliberately **no PKCE**, since
  nothing here auto-enables it the way that library did for Gmail, which is what caused the
  "Missing code verifier" bug during the Gmail rollout). Current accounts: `support_zoho`
  (`client_type='legacy'`).
- **Entry point**: `process_zoho(account, *, dry_run, limit, safety_overlap_days, classify)`.
- **Incremental fetch**: cursor converted to epoch-ms and applied **client-side** — Zoho's server-side date filters were found unreliable.
- **Pagination**: offset paging, `PAGE_SIZE=200`.
- Attachment info requires a separate API call even when `hasAttachment` is set (and that field is the *string* `"1"`/`"0"`, not a bool — a real gotcha noted directly in the code).

### Fireflies (`fireflies_fetcher.py`) — call transcripts

- **API**: GraphQL, `https://api.fireflies.ai/graphql`.
- **Auth**: single account-level API key (`FIREFLIES_API_KEY`), `Authorization: Bearer`.
- **Entry point**: `process_fireflies(*, dry_run, limit, safety_overlap_days, classify)`.
- **Incremental fetch**: Fireflies has **no server-side date filter** — every stub is listed, then filtered client-side against the cursor.
- **Pagination**: `skip += 50` until a short page.
- Dedups against existing rows *before* fetching full transcript detail, to save an API call.

### Fathom (`fathom_fetcher.py`) — call recordings

- **API**: REST, `https://api.fathom.ai/external/v1`.
- **Auth**: single API key (`RON_FATHOM_API_KEY`), `X-Api-Key` header.
- **Entry point**: `process_fathom(*, dry_run, limit, safety_overlap_days, classify)`.
- **Incremental fetch**: cursor passed as a real server-side `created_after` param (unlike Fireflies/Zoho).
- **Pagination**: cursor-based, following `next_cursor`. Mandatory 2-second pause between calls (rate-limit courtesy).
- `client_id` is always `None` for Fathom calls — all Fathom recordings are treated as unmatched/general, by design.

### Client implementation boards (`odoo_fetcher.py`) — 6 clients, 6 Odoo instances

- **API**: Odoo XML-RPC (`/xmlrpc/2/common` for auth, `/xmlrpc/2/object` for `execute_kw`), models `project.task`, `mail.message`, `ir.attachment`, `project.project`.
- **Auth**: one username/password pair per client (a genuinely separate Odoo instance per client).
- **Entry point**: `process_client(cfg, *, dry_run)`, batched via `process_all(*, dry_run, only_client)`.
- **Not incremental** — this is a deliberate **full refresh every run** (no cursor gates anything). Rationale straight from the docstring: *"this data is entirely Odoo-derived and never hand-edited, so there's no human-edit-protection concern and no cursor/dedup complexity needed."* A `sync_cursors` row is still written per client after each run, purely for observability parity — nothing reads it back.
- Deactivated/removed tasks are handled by `mark_tasks_inactive()` (soft-delete via `active=false`), not a hard delete.
- Skips the access-tier LLM call entirely for tasks already seen before (since it's excluded from upsert anyway — a deliberate cost-saving choice, since re-classifying would produce a result that's immediately discarded).

**The 6 configured clients (`ODOO_CLIENTS`):**

| Client slug | Odoo base URL | Odoo DB | Kanban project name(s) |
|---|---|---|---|
| `greer-steel` | greersteel.eoxs.com | `greer` | CRM Implementation |
| `eastern-states-steel` | ess.eoxs.com | `ess` | Implementation - Phase 1 |
| `discount-pipe-steel` | discountpipesteel.eoxs.com | `discount_pipe2` | Implementation |
| `ppc-metals` | ppc.eoxs.com | `velox` | Implementation Tasks |
| `3gm-steel` | 3gm.eoxs.com | `threegmsteels` | 1. Kick off/Discovery Calls; 5. Master Data Import |
| `sabre-alloys` | sabre.eoxs.com | `sabre` | Soft Launch |

Two clients in the `clients` table (`rw-conklin-steel`, `brannon-steel`) have **no** Odoo instance configured (`odoo_base_url`/`odoo_db` both NULL) — no implementation-task ingestion is possible for them; they only appear via other sources (email, calls).

### EOXS Support Tickets & Invoices — removed from Cruz's MCP tool surface AND from ingestion (2026-08-10, sweep fix 2026-08-12)

Support tickets and invoices/sales-orders were deliberately removed from this system — `get_ticket`/`search_tickets`/`get_invoice`/`search_invoices` no longer exist as MCP tools (see `docs/backend-server.md` §5), and every historical row in `tickets`/`sales_orders`/`invoices` (and their child tables) was deleted. That data now lives exclusively in the separate `eoxs-teams` Odoo connector.

**RESOLVED 2026-08-12** (was an open bug from 2026-08-10 to 2026-08-12): the 2026-08-10 removal only reached the MCP tool surface and (for implementation tasks specifically) `run_full_sweep()`'s per-client odoo source list — `tickets_fetcher.py`/`invoice_fetcher.py`'s own registration in the recurring sweep (`ingestion/server.py`'s `run_full_sweep()` source list) was never updated to match, so the sweep kept calling `process_tickets()` every 2 hours regardless, writing rows nobody could query through any Cruz tool anymore (`invoice_fetcher.py`, by contrast, had already stopped producing rows on its own — `sales_orders`/`invoices` stayed at 0 the whole time, an unexplained asymmetry that turned out not to matter once both were removed from the sweep). Fixed by removing both entries from the sweep list, same pattern already used for `odoo` just above it. The 17 stray `tickets` rows this wrote between 2026-08-10 and 2026-08-12 (100% of what was in the table — none of it legitimate, none of it reachable via any tool) were deleted along with their `ticket_events`/`ticket_attachments` children.

### EOXS Support Tickets (`tickets_fetcher.py`) — central Odoo instance

- **API**: same Odoo XML-RPC mechanism, but a **separate, central** instance — `BASE_URL = "https://teams.eoxs.com"`, `DB = "Eoxteams_12Feb24"` — filtered to a single project, `project.task` where `project_id = 76` ("EOXS Support"). This is explicitly **not** one of the 6 per-client instances above.
- **Auth**: `EOXS_TICKETS_ODOO_USERNAME`/`PASSWORD`.
- **Entry point**: `process_tickets(*, dry_run, limit, safety_overlap_days)`.
- **Incremental fetch**: real cursor, `project.task.write_date >= since`, `sync_cursors` source `"eoxs_tickets"`.
- Ticket numbers are formatted as `T{task_id:05d}` (e.g. `T00076`). Ported from an n8n workflow ("support ticket wiki ingestion v10") the user provided directly, with no prior Python reference for this source.

### Sales Orders & Invoices (`invoice_fetcher.py`) — same central instance as tickets

- **API**: same instance as tickets (`teams.eoxs.com` / `Eoxteams_12Feb24`), models `sale.order`, `sale.order.line`, `mail.message`, `account.move`, `account.move.line`. **Not** project/Kanban-filtered — pulled directly from Sales and Accounting.
- **Auth**: reuses the same `EOXS_TICKETS_ODOO_USERNAME`/`PASSWORD` as tickets.
- **Entry point**: `process_invoices(*, dry_run, limit, safety_overlap_days)`.
- **Incremental fetch**: real cursor, `sale.order.write_date >= since`, `sync_cursors` source `"eoxs_invoices"`.
- Ported from a second n8n workflow ("invoice wiki ingestion automation v4"). 185 historical rows pre-existed from an earlier file-based loader; they get "graduated" into live-maintained rows via upsert on `order_number` the first time Odoo reports a change — no duplication.

## 3. Writers (`write_*.py`)

| File | Function | Writes to | Upsert key | Deletes+reinserts children? |
|---|---|---|---|---|
| `write_email.py` | `write_thread()` | email_threads, email_messages, email_attachments | `(source_account, gmail_thread_id)` | yes |
| `write_call.py` | `write_call()` | call_transcripts, call_segments | `(source, external_id)` where path is NULL | yes |
| `write_ticket.py` | `write_ticket()` | tickets, ticket_events, ticket_attachments | `ticket_number` | yes |
| `write_invoice.py` | `write_sales_order()` | sales_orders + 4 child tables | `order_number` | yes (invoice_lines cascades via FK) |
| `write_implementation.py` | `write_client_tasks()` + `mark_tasks_inactive()` | implementation_tasks + children | `(client_id, odoo_task_id)` | children yes, **parent tasks no** (see below) |

Universal rule: **`access_tier` is excluded from every `DO UPDATE SET` clause**, on every writer — once a row is classified, its tier survives every later refresh.

`write_implementation.py` is the one writer that changed strategy mid-life: it used to wipe the whole `implementation_tasks` table per client on every run, which was silently resetting `access_tier` back to the column default every 2 hours. It was rewritten to upsert + soft-delete reconciliation (`mark_tasks_inactive`) specifically to stop that — a good real-world example of the tiering system constraining write-layer design.

## 4. The `dual_write()` mechanism (`ingestion/db.py`)

```python
def dual_write(write_fn, *args, **kwargs):
    live_conn = get_live_conn()
    try:
        result = write_fn(live_conn, *args, **kwargs)
    finally:
        live_conn.close()
    try:
        staging_conn = get_staging_conn()
        try:
            write_fn(staging_conn, *args, **kwargs)
        finally:
            staging_conn.close()
    except Exception as e:
        logging.getLogger("ingestion.db").warning(
            "staging write failed (live succeeded, continuing): %s", e)
    return result
```

**Live is mandatory** — any exception there propagates up and aborts the item being processed. **Staging is best-effort** — any failure is caught, logged as a warning, and swallowed; its return value is discarded entirely. Design intent, straight from the module: *"live must never wait on staging's health."* Every writer must accept `conn` as its first argument and commit internally so `dual_write()` can call it twice with identical arguments.

## 5. `sync_cursors` — how incremental fetching actually works

Table: `sync_cursors(source TEXT PRIMARY KEY, last_synced_at TIMESTAMPTZ, updated_at TIMESTAMPTZ)`. Logic in `ingestion/state.py`:

- `sync_since(source, safety_overlap_days=2)` → `last_synced_at - 2 days`, or `None` if never synced. The 2-day overlap absorbs clock skew and late-arriving webhooks.
- Each fetcher captures `run_started_at` at the **top** of its run, does all its work, then (if not a dry run) calls `set_last_synced_at(source, run_started_at)` — stamping the cursor to the run's *start*, not its completion, so nothing that arrived mid-run gets silently skipped.

**A real, unmitigated edge case**: the cursor advances **unconditionally**, even when `--limit` truncates the result set below the true number of new items. If you run a fetcher with `--limit 50` and there were actually 300 new rows upstream, only 50 get processed, but the cursor still jumps to `run_started_at` — the other 250 are **not** re-caught by the next run's `since` filter (only the fixed 2-day overlap would help, and only if it happens to still cover them). Every incremental fetcher (Gmail, Zoho, Fireflies, Fathom, tickets, invoices) has this behavior. If you're doing a manual backlog catch-up with `--limit`, be aware the cursor doesn't know it was truncated — prefer an unbounded run, or reset the cursor row (`DELETE FROM sync_cursors WHERE source = '<name>'`) before an unbounded backfill.

`odoo_fetcher.py` writes a cursor row per client too, but — as its own docstring says — nothing ever reads it back (full refresh every run regardless).

## 6. Dedup — two separate mechanisms at two separate layers

**Raw layer** — `message_ids_seen(message_id TEXT PRIMARY KEY, thread_source_account TEXT, thread_id INT, seen_at TIMESTAMPTZ)`. Used only by `gmail_fetcher.py` and `zoho_fetcher.py`: `mark_messages_seen()` records every message id ever written; a thread is skipped entirely if **every** message in it has already been seen elsewhere (cross-thread dedup — the same message appearing in two different thread views shouldn't create two DB rows). Calls use a simpler direct existence check instead (`existing_call()` on `(source, external_id)`), since a transcript is immutable once created.

**Wiki layer** — `wiki_ingest_seen` — belongs entirely to the separate wiki-synthesis pipeline (see `docs/wiki-ingestion.md`), not to raw ingestion. It's content-hash based (detects *changes* to an already-seen raw row) rather than first-sight based. The two tables never share logic: `message_ids_seen` gates whether a raw fetcher writes a row at all; `wiki_ingest_seen` gates whether the wiki pipeline re-processes an already-written raw row.

## 7. Filtration & classification

Three distinct classifiers, all calling `claude-haiku-4-5-20251001`:

- **`spam_filter.py`** (`is_eoxs_relevant()`) — Gmail/Zoho only. KEEP/DISCARD; discards marketing/newsletter/cold-outreach/automated-receipts/phishing only. **Fails open** (any error → keep) — a filtering mistake should never lose real data.
- **`call_relevance.py`** (`is_call_relevant()`) — Fireflies/Fathom only. A call with zero overview/topics/actions/excerpt is discarded **without spending an API call at all**. Otherwise: keep anything with real conversational content. **Fails open**.
- **`inline_tier_classifier.py`** (`classify_tier()`) — runs once per new row, across every source (emails, calls, tickets, tasks, orders). Three-way verdict: `tier1` (Raj-personal), `tier2_confidential` (company-confidential: salary/payroll/investor-relations/financials/vendor-pricing/legal), `tier2` (everything else, the default). **Fails closed** (any error → `tier1`, the most restrictive). Uses a *separate* API key, `CLASSIFIER_ANTHROPIC_API_KEY`, kept apart from the spam/relevance filters' `ANTHROPIC_API_KEY` purely so its cost is independently trackable.

Classification only happens on a row's first INSERT (it's excluded from every `DO UPDATE SET`) — a known limitation is that content added *later* to an already-classified thread (say, a salary figure showing up in message #5) won't retroactively change that row's tier without a rerun of the bulk backlog job.

**Bulk backlog reclassifier** — `ingestion/tier_classifier.py` — a full async re-scan across `email_threads`, `call_transcripts`, `tickets`, `implementation_tasks`, `sales_orders`, same 3-tier prompt, `CONCURRENCY=10`, writing one `UPDATE` per row **immediately** after each verdict (not batched at the end — an earlier version lost all progress when killed mid-run because it only wrote once at the very end; this was fixed to write incrementally).

**Roughly how many AI calls happen per new row**: emails ≈ up to 2 (spam filter + tier classifier); calls ≈ up to 2 (relevance filter + tier classifier, fewer if the call is empty); tickets/tasks/orders ≈ 1 (tier classifier only — no spam/relevance filter applies to Odoo-derived sources); implementation tasks already seen before ≈ 0 (classification is explicitly skipped since it would be discarded anyway).

## 8. Webhook vs. sweep — exactly what's real-time and what isn't

Straight from `ingestion/server.py`'s own docstring: *"Zoho and Odoo have no webhook in this system (same as the old pipeline) -- they, and everything else, get picked up by the daily full-sweep cron fallback."*

| Source | Real-time webhook? | Fallback |
|---|---|---|
| Gmail (every `raw_sweep_enabled` account) | Yes — Google Cloud Pub/Sub push, `/webhook/gmail` (refetches every active account, since the push payload doesn't cheaply map to one specific account) | 2-hour sweep |
| Fireflies | Yes — `transcript.completed`, `/webhook/fireflies`, HMAC-SHA256-verified if `FIREFLIES_WEBHOOK_SECRET` set | 2-hour sweep |
| Fathom | Yes — `recording.completed`, `/webhook/fathom`, Svix-signature-verified if `FATHOM_WEBHOOK_SECRET` set | 2-hour sweep |
| Zoho | **No** | 2-hour sweep only |
| EOXS Support Tickets | **No** | 2-hour sweep only |
| Sales Orders & Invoices | **No** | 2-hour sweep only |
| Client implementation boards (all 6) | **No** | 2-hour sweep only |

A global `asyncio.Lock` prevents overlapping runs (webhook or sweep) — a trigger that arrives while another is in-flight is simply dropped with a log line, not queued. Every trigger, webhook or sweep, is recorded in `ingest_log`; only full-sweep runs (`/trigger/manual` or the cron `--sweep` path) additionally report to Linear, deliberately, so a single incoming email doesn't flood the Linear board with an issue per message.

The actual production 2-hourly sweep runs via `python -m ingestion.server --sweep` (triggered by `deploy/eoxs-sweep.timer`), not the HTTP `/trigger/manual` route — see `docs/backend-server.md` for the exact timer schedule.

## 9. Client matching (`client_id`) — two different strategies

**Participant-email-based** (`ingestion/routing.py`) — used by Gmail, Zoho, Fireflies. `load_client_index()` builds `{emails, domains, slugs}` maps from `clients.domains` and `contacts.email`. `classify_client()` tries an exact contact-email match first, falls back to matching the sender's domain, and returns "unmatched/general" if neither hits. **Not** used by Fathom (always unmatched by design).

**Freetext/structural matching** — used by the Odoo-derived sources:
- `odoo_fetcher.py` already knows the client (it's iterating a specific client's config) — resolves `client_id` with a direct `slug` lookup.
- `tickets_fetcher.py` / `invoice_fetcher.py` (both hitting the shared central Odoo instance covering many clients) resolve `client_id` via `display_name ILIKE` against the Odoo record's partner display name — since ticket/order "client_raw" is freetext, not an email/domain.

## 10. `ingest_log` — the audit trail

`ingest_log(id, log_date, operation, description, raw_entry)`, written exclusively by `ingestion/ingest_log.py`'s `log_run(trigger_source, result)`, called after every webhook or sweep completes. Handles both a single fetcher's result shape and the full sweep's multi-source shape. **Never raises** — a logging failure must never mask an otherwise-successful ingestion run. Dual-written like everything else. This is the complete, granular record of every trigger, webhook or sweep — distinct from Linear reporting, which only fires for full sweeps (see `docs/linear-integration.md`).

## 11. Real current row counts (live `eoxs_wiki`, as of 2026-08-12)

| Table | Rows |
|---|---|
| email_threads | 30,489 |
| email_messages | 59,728 |
| email_attachments | 20,578 |
| call_transcripts | 2,387 |
| call_segments | 179,058 |
| tickets | 14 (still growing — §12, known bug) |
| ticket_events | 104 |
| ticket_attachments | 1 |
| implementation_tasks | 828 |
| implementation_task_events | 5,613 |
| implementation_task_attachments | 686 |
| sales_orders | 0 |
| order_lines | 0 |
| sales_order_events | 0 |
| invoices | 0 |
| invoice_lines | 0 |
| clients | 8 |
| contacts | 56 |

## 12. Three things worth flagging to a new developer

1. **The `--limit` + cursor-advance interaction (§5)** — a real gap. Don't do manual backlog catch-ups with `--limit` without either accepting the gap or resetting the cursor first.
2. **Two Odoo "worlds" that must not be confused**: 6 **per-client** implementation-board instances (`odoo_fetcher.py`, one Odoo tenant per client) vs. 1 **central** `teams.eoxs.com` instance shared by tickets and invoices (`tickets_fetcher.py`/`invoice_fetcher.py`) — same underlying Odoo models, completely different databases/credentials/routing logic. `ingestion/_check_env.py` (the project's own env-var-completeness diagnostic) is also a bit stale in places (see the Linear key-name note in `docs/linear-integration.md`) — when in doubt about which env var a given piece of code actually reads, grep the fetcher itself rather than trusting the diagnostic script.
3. **`tickets_fetcher.py` is still live in the recurring sweep despite tickets being removed from Cruz's tool surface (§2 above)** — a real, unresolved bug, not a documentation lag. If you're touching sweep code, this is the next thing that should get fixed: find and remove `tickets_fetcher.py`'s call from `ingestion/server.py`'s sweep path, matching what already correctly happened to `invoice_fetcher.py` and the per-client odoo/implementation-task source.
