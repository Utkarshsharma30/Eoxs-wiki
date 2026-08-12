# Postgres Database

*Deep technical reference for anyone working directly with Cruz's (`eoxs-wiki-db`) database.*

Server: PostgreSQL 16.14 (Ubuntu). Two databases exist: **`eoxs_wiki`** (live) and **`eoxs_wiki_staging`** (a full physical mirror, used only for raw-ingestion dual-write safety — see §8). Connection details live in `.env` as `PGHOST`/`PGPORT`/`PGDATABASE`/`PGDATABASE_STAGING`/`PGUSER`/`PGPASSWORD` (values never repeated in this document).

## 1. How the schema was built — every migration, in order

All schema changes are numbered, version-controlled SQL files in `/home/deploy/eoxs-wiki-db/schema/`, applied in order and tracked (never re-applied) via `schema/run_migrations.py` and a `schema_migrations` bookkeeping table.

| # | File | What it does |
|---|---|---|
| 001 | `001_extensions.sql` | Enables `pg_trgm` — fuzzy/ILIKE title search. |
| 002 | `002_reference.sql` | `clients` (slug, display_name, domains[], odoo_base_url/db) and `contacts` (client_id FK, name, email, is_relay_inbox). |
| 003 | `003_emails.sql` | `email_threads`, `email_messages` (generated `body_tsv` full-text column), `email_attachments`. |
| 004 | `004_wiki.sql` | `wiki_pages`, `wiki_links` (the `[[wikilink]]` graph), `wiki_citations` (polymorphic), `wiki_flags`. |
| 005 | `005_operational.sql` | `ingest_log`, `db_sync_state`. |
| 006 | `006_wiki_title_not_unique.sql` | Drops the uniqueness constraint on wiki page titles — legitimate pre-existing duplicate titles exist. |
| 007 | `007_wiki_updated_raw.sql` | Adds `wiki_pages.updated_raw` (preserves the original frontmatter `updated:` text alongside the parsed date). |
| 008 | `008_tickets.sql` | `tickets`, `ticket_events`, `ticket_attachments`. |
| 009 | `009_invoices.sql` | `sales_orders`, `order_lines` (historical file-based data). |
| 010 | `010_calls.sql` | `call_transcripts` (Fireflies + Fathom unified), `call_segments`. |
| 011 | `011_ingestion_state.sql` | `sync_cursors`, `message_ids_seen` — the DB-native raw-ingestion service's bookkeeping. |
| 012 | `012_nullable_source_file_path.sql` | Makes `source_file_path` nullable on 4 tables — API-fetched rows have no filesystem path. |
| 013 | `013_call_transcripts_api_natural_key.sql` | Adds a partial unique index fixing a real dedup bug where `NULL source_file_path` broke `ON CONFLICT` for API-fetched calls. |
| 014 | `014_implementation_tasks.sql` | `implementation_tasks`, `implementation_task_events`, `implementation_task_attachments`. |
| 015 | `015_nullable_attachment_relative_path.sql` | Relaxes `email_attachments.relative_path` — DB-native fetchers store metadata only. |
| 016 | `016_nullable_ticket_attachment_relative_path.sql` | Same relaxation for tickets, plus adds `mimetype`/`size_bytes`. |
| 017 | `017_wiki_ingestion.sql` | Creates the **`wiki_staging` schema** (draft-review workspace, inside `eoxs_wiki` itself) plus `wiki_ingest_cycles`, `wiki_ingest_batches`, `wiki_ingest_seen`. |
| 018 | `018_wiki_pages_db_native.sql` | Makes wiki-page file-path columns nullable (DB-native pages have no file backing). |
| 019 | `019_access_tier.sql` | Creates the `access_tier` enum (`tier1`/`tier2`) and adds it to 7 tables. |
| 020 | `020_tier2_confidential.sql` | Adds `'tier2_confidential'` to the enum — redesigns 2 tiers into today's 3. |
| 021 | `021_wiki_board_state.sql` | `wiki_ingest_board_state` — tracks persistent Linear "board" issue IDs. |
| 022 | `022_invoices_live.sql` | Makes invoice-related columns nullable for live-only orders; adds `sales_order_events`, `invoices`, `invoice_lines`. |
| 023 | `023_mcp_redaction_log.sql` | `mcp_redaction_log` — audit trail for every time the MCP server's query-time redaction safety net actually strips content (see `docs/backend-server.md` §5). |
| 024 | `024_email_attachment_extraction.sql` | Adds `source_attachment_id`, `mimetype`, `extracted_text` to `email_attachments` — closes the gap where attachment content was structurally invisible to every MCP tool. |

## 2. Every table, grouped logically

**36 base tables total**, across 2 schemas (`public` and `wiki_staging`). No other schemas exist.

- **Reference:** `clients`, `contacts`
- **Emails:** `email_threads`, `email_messages`, `email_attachments`
- **Wiki (live):** `wiki_pages`, `wiki_links`, `wiki_citations`, `wiki_flags`
- **Tickets:** `tickets`, `ticket_events`, `ticket_attachments` — **no longer reachable via any MCP tool** (removed 2026-08-10, historical rows deleted); the tables still exist because a known bug in the recurring sweep still writes new rows to `tickets`/`ticket_events` — see `docs/raw-ingestion.md` §12.
- **Sales/Invoices:** `sales_orders`, `order_lines`, `sales_order_events`, `invoices`, `invoice_lines` — same MCP-tool removal as tickets; currently empty (0 rows) and, unlike tickets, not being repopulated.
- **Calls:** `call_transcripts`, `call_segments`
- **Implementation tasks:** `implementation_tasks`, `implementation_task_events`, `implementation_task_attachments`
- **Operational/bookkeeping:** `ingest_log`, `db_sync_state`, `sync_cursors`, `message_ids_seen`, `schema_migrations`, `wiki_ingest_cycles`, `wiki_ingest_batches`, `wiki_ingest_seen`, `wiki_ingest_board_state`, `mcp_redaction_log`
- **`wiki_staging` schema (draft review workspace):** `wiki_staging.wiki_pages`, `wiki_staging.wiki_links`, `wiki_staging.wiki_citations`, `wiki_staging.wiki_flags`

A sibling database, `eoxs_frontend_threads` (same Postgres instance, different logical database), belongs to the separate `eoxs-frontend-threads` repo — not documented here, see that repo's own `README.md`.

## 3. Full column structure

No `CHECK` constraints exist anywhere — `access_tier` is enforced purely as a Postgres **ENUM type**, not a check constraint.

**`access_tier` enum**, exact values in ordinal order: `tier1`, `tier2_confidential`, `tier2`. (`tier2_confidential` sorts *before* `tier2` in ordinal position because it was added via `ALTER TYPE ... ADD VALUE 'tier2_confidential' AFTER 'tier1'` — don't rely on alphabetical/ordinal sort implying anything about restrictiveness.)

Other enums: `call_source` (`fireflies`, `fathom`), `email_source_account` (`raj_gmail`, `ron_gmail`, `remya_gmail`, `support_zoho`), `wiki_flag_type` (`contradiction`, `unverified`), `wiki_page_type` (`entity`, `concept`, `source`, `analysis`, `overview`, `prospect`).

Per-table structure (columns, types, keys) as it exists right now:

```
clients: id serial PK, slug text UNIQUE NOT NULL, display_name text NOT NULL,
  domains text[], odoo_base_url text, odoo_db text, created_at/updated_at timestamptz

contacts: id serial PK, client_id int FK->clients, name text NOT NULL, email text,
  is_relay_inbox bool DEFAULT false, created_at/updated_at timestamptz

email_threads: id serial PK, source_account email_source_account NOT NULL,
  gmail_thread_id text NOT NULL, subject/from_addr/to_addr text, message_count int,
  participants text[], thread_dates timestamptz[], tags text[], is_quarantined bool,
  generated_at timestamptz, source_file_path text NULL, client_id int FK->clients,
  access_tier access_tier NOT NULL DEFAULT 'tier1'
  UNIQUE (source_account, gmail_thread_id)

email_messages: id serial PK, thread_id int NOT NULL FK->email_threads,
  message_index int NOT NULL, message_date timestamptz, from_addr text, body text NOT NULL,
  body_tsv tsvector (generated)
  UNIQUE (thread_id, message_index)

email_attachments: id serial PK, thread_id int NOT NULL FK->email_threads,
  message_id int FK->email_messages, filename text NOT NULL, relative_path text NULL,
  size_bytes bigint, note text, source_attachment_id text NULL, mimetype text NULL,
  extracted_text text NULL

wiki_pages: id serial PK, title text NOT NULL, page_type wiki_page_type NOT NULL,
  entity_class text, tags text[], sources_raw text[], created_date/updated_date date,
  updated_raw text, generated_hash text, body text NOT NULL, body_tsv tsvector (generated),
  source_file_path text NULL UNIQUE, access_tier access_tier NOT NULL DEFAULT 'tier1'

wiki_links: id serial PK, from_page_id int NOT NULL FK->wiki_pages,
  to_page_id int NULL FK->wiki_pages (nullable — unresolved link target),
  to_title_raw text NOT NULL, display_text text, context_snippet text

wiki_citations: id serial PK, wiki_page_id int NOT NULL FK->wiki_pages,
  source_type text NOT NULL, source_id int NULL, source_ref_raw text NOT NULL
  -- polymorphic: NO database-level FK on (source_type, source_id), since the
  -- target table varies (email_thread / ticket / call_transcript / implementation_task)

wiki_flags: id serial PK, wiki_page_id int NOT NULL FK->wiki_pages,
  flag_type wiki_flag_type NOT NULL, text text NOT NULL

tickets: id serial PK, odoo_id int NULL, ticket_number text NOT NULL UNIQUE,
  client_raw text, client_id int FK->clients, subject/status/priority/assigned_to text,
  ticket_created/ticket_closed date, tags text[], description text, generated_at timestamptz,
  source_file_path text NULL, access_tier access_tier NOT NULL DEFAULT 'tier2'

ticket_events: id serial PK, ticket_id int NOT NULL FK->tickets, odoo_msg_id bigint,
  event_type text NOT NULL, author text, event_time timestamptz, body text NOT NULL,
  event_order int NOT NULL

ticket_attachments: id serial PK, ticket_id int NOT NULL FK->tickets, filename text NOT NULL,
  relative_path text NULL, mimetype text, size_bytes bigint

sales_orders: id serial PK, odoo_id int NULL, order_number text NOT NULL UNIQUE,
  client_raw text, client_id int FK->clients, order_date/expiry_date date,
  amount_total numeric(14,2), currency text, state/state_label text, salesperson text,
  client_order_ref text, linked_invoices text[], generated_at timestamptz,
  source_file_path text NULL, access_tier access_tier NOT NULL DEFAULT 'tier2'

order_lines: id serial PK, sales_order_id int NOT NULL FK->sales_orders, product/description text,
  qty/delivered/invoiced/unit_price/discount_pct/subtotal numeric(14,2), line_order int NOT NULL

sales_order_events: id serial PK, sales_order_id int NOT NULL FK->sales_orders,
  odoo_msg_id int, message_type/author text, event_time timestamptz, body text NOT NULL,
  event_order int NOT NULL

invoices: id serial PK, sales_order_id int NOT NULL FK->sales_orders, odoo_id int NOT NULL,
  invoice_number/state text, invoice_date/due_date date,
  amount_untaxed/amount_tax/amount_total/amount_paid/amount_due numeric(14,2),
  payment_state text, invoice_sent bool, narration text
  UNIQUE (sales_order_id, odoo_id)

invoice_lines: id serial PK, invoice_id int NOT NULL FK->invoices, product/description text,
  qty/unit_price/discount_pct/subtotal numeric(14,2), line_order int NOT NULL

call_transcripts: id serial PK, source call_source NOT NULL, external_id text NOT NULL,
  meeting_title text, call_date date, duration_seconds bigint, duration_human/host_email text,
  participants text[], recording_url text, fireflies_summary text,
  key_topics/action_items/tags text[], generated_at timestamptz, client_id int FK->clients,
  source_file_path text NULL, transcript_body text NOT NULL, transcript_tsv tsvector (generated),
  access_tier access_tier NOT NULL DEFAULT 'tier1'
  UNIQUE (source, external_id, source_file_path); UNIQUE (source_file_path)
  Partial UNIQUE (source, external_id) WHERE source_file_path IS NULL

call_segments: id serial PK, call_id int NOT NULL FK->call_transcripts,
  segment_order int NOT NULL, speaker text, text text NOT NULL

implementation_tasks: id serial PK, client_id int NOT NULL FK->clients, odoo_task_id int NOT NULL,
  project_name/task_name text NOT NULL, stage/owner/priority/kanban_state text, active bool,
  description text, task_created_date/task_updated_date/deadline date, generated_at timestamptz,
  access_tier access_tier NOT NULL DEFAULT 'tier2'
  UNIQUE (client_id, odoo_task_id)

implementation_task_events: id serial PK, task_id int NOT NULL FK->implementation_tasks,
  odoo_msg_id bigint, event_type/author text, event_time timestamptz, body text NOT NULL,
  tracking_changes jsonb DEFAULT '[]', event_order int NOT NULL

implementation_task_attachments: id serial PK, task_id int NOT NULL FK->implementation_tasks,
  odoo_attachment_id int NOT NULL, filename text NOT NULL, mimetype text, size_bytes bigint

ingest_log: id serial PK, log_date date NOT NULL, operation text NOT NULL,
  description text NOT NULL, raw_entry text NOT NULL

db_sync_state: source_type text NOT NULL, source_file_path text NOT NULL,
  file_mtime timestamptz NOT NULL, file_hash text, last_loaded_at timestamptz
  PK (source_type, source_file_path)

sync_cursors: source text PK, last_synced_at timestamptz, updated_at timestamptz

message_ids_seen: message_id text PK, thread_source_account text NOT NULL,
  thread_id int NULL, seen_at timestamptz

schema_migrations: filename text PK, applied_at timestamptz
  -- bookkeeping only, created by run_migrations.py itself, not one of the numbered files

wiki_ingest_cycles: id serial PK, started_at/finished_at timestamptz,
  status text DEFAULT 'running', summary jsonb NULL, linear_parent_issue_id text
  -- linear_parent_issue_id is DEAD: never read or written by any code (see docs/linear-integration.md)

wiki_ingest_batches: id serial PK, cycle_id int NOT NULL FK->wiki_ingest_cycles,
  source_kind text NOT NULL, status text DEFAULT 'running', row_count int DEFAULT 0,
  linear_issue_id text, started_at/finished_at timestamptz, error text
  -- linear_issue_id here is likewise DEAD, same reason

wiki_ingest_seen: source_kind text, source_row_id int, content_hash text NOT NULL,
  decision text NOT NULL ('processed' | 'skipped_unchanged' | 'skipped_noise'),
  last_seen_at timestamptz, cycle_id int FK->wiki_ingest_cycles ON DELETE SET NULL
  PK (source_kind, source_row_id)

wiki_ingest_board_state: board_key text PK, linear_issue_id text NOT NULL,
  linear_issue_identifier text NOT NULL, updated_at timestamptz
  -- ONE row today: board_key='pending_drafts'

mcp_redaction_log: id serial PK, occurred_at timestamptz NOT NULL DEFAULT now(),
  clearance_name text NOT NULL ('hr' | 'general' -- never 'full', that identity skips
  the redaction check entirely), tool_name text NOT NULL,
  redacted_snippets text[] NOT NULL -- the exact spans that were removed

wiki_staging.wiki_pages: id serial PK, live_page_id int NULL (informal, no FK — points at
  public.wiki_pages.id once promoted), title text NOT NULL, page_type wiki_page_type NOT NULL,
  entity_class text, tags/sources_raw text[], body text NOT NULL, source_kind text NOT NULL,
  cycle_id int NOT NULL (informal, no FK), status text DEFAULT 'draft'
  ('draft' | 'reviewed' | 'rejected' | 'promoted'), review_notes text NULL,
  access_tier access_tier NOT NULL DEFAULT 'tier1'

wiki_staging.wiki_links / wiki_citations / wiki_flags: same shapes as their public.wiki_*
  counterparts, but FK targets point at wiki_staging.wiki_pages instead of public.wiki_pages.
```

## 4. Indexes

Every FK column has a supporting btree index (60+ total across the database), plus:

- **Fuzzy/trigram search (GIN, `gin_trgm_ops`):** `idx_email_threads_subject_trgm`, `idx_tickets_subject_trgm`, `idx_wiki_pages_title_trgm`
- **Full-text search (GIN, tsvector):** `idx_email_messages_tsv`, `idx_wiki_pages_tsv`, `idx_call_transcripts_tsv`
- **Partial indexes:** `idx_call_transcripts_api_natural_key` (UNIQUE on `(source, external_id) WHERE source_file_path IS NULL`); `idx_wiki_links_unresolved` (`wiki_links(to_title_raw) WHERE to_page_id IS NULL`)
- **`access_tier`** is plain-btree indexed (not GIN) on every table that has it: `email_threads`, `call_transcripts`, `tickets`, `implementation_tasks`, `sales_orders`, `wiki_pages`, `wiki_staging.wiki_pages`

## 5. Extensions

Only two exist, identically in both databases: `plpgsql` (bundled default) and `pg_trgm` v1.6 (the only intentionally-added one — powers all fuzzy/trigram search). No other extensions were ever installed.

## 6. Roles & privileges

| Role | Superuser | Login | Real privileges |
|---|---|---|---|
| `postgres` | yes | yes | system superuser |
| `eoxs_app` | no | yes | **Owns all 35 tables.** Full DML (SELECT/INSERT/UPDATE/DELETE/TRUNCATE/REFERENCES/TRIGGER). Used by every writer AND currently by the MCP server too (see the gap noted in `docs/backend-server.md` §5). |
| `eoxs_readonly` | no | yes | `GRANT SELECT` only, on every table in both `public` and `wiki_staging` (confirmed: 100% coverage, no INSERT/UPDATE/DELETE anywhere). `ALTER DEFAULT PRIVILEGES FOR ROLE eoxs_app` ensures any future table auto-grants SELECT to this role too. Used exclusively by pgweb, which is itself further restricted by its own `--readonly`/`--lock-session` flags and sits behind nginx basic auth — three independent layers. |

No other application-relevant roles exist (the rest are Postgres 16's built-in `pg_*` predefined roles, unused here).

## 7. Real current row counts (live `eoxs_wiki`, as of 2026-08-12)

| Table | Rows |
|---|---|
| call_segments | 179,058 |
| email_messages | 59,728 |
| email_threads | 30,489 |
| email_attachments | 20,578 |
| wiki_links | 13,628 |
| call_transcripts | 2,387 |
| wiki_pages | 1,348 |
| implementation_tasks | 828 |
| wiki_citations | 2,196 |
| wiki_flags | 1,064 |
| implementation_task_events | 5,613 |
| implementation_task_attachments | 686 |
| mcp_redaction_log | 335 |
| ticket_events | 104 |
| tickets | 14 |
| ticket_attachments | 1 |
| sales_orders | 0 |
| order_lines | 0 |
| sales_order_events | 0 |
| invoices | 0 |
| invoice_lines | 0 |
| clients | 8 |
| contacts | 56 |
| sync_cursors | 28 |
| ingest_log | 117 |
| schema_migrations | 24 |
| wiki_ingest_cycles | 39 |
| wiki_ingest_batches | 190 |
| wiki_ingest_board_state | 1 |
| wiki_staging.wiki_pages | 425 |

`tickets`/`ticket_events` are **not zero and not static** — 14 rows now, up from 2 a few hours prior at the time of writing, direct live evidence of the known lingering sweep bug (`docs/raw-ingestion.md` §12): the MCP-tool-surface removal and the historical-row deletion both happened, but the recurring sweep's `tickets_fetcher.py` invocation was never actually removed, so it keeps writing new rows nobody can query through Cruz anymore. `sales_orders`/`invoices` show the fully-removed, non-regrowing state — the fix that worked for one didn't get applied to the other.

**`access_tier` breakdown, `wiki_pages` (live):** 39 tier1 / 753 tier2_confidential / 556 tier2.

**`wiki_staging.wiki_pages` status (425 total):** 21 rejected, 404 promoted, 0 draft, 0 reviewed — the multi-hundred-page promotion backlog documented as a known issue in earlier snapshots of this system has since been fully cleared; see `docs/wiki-ingestion.md` §8.

## 8. Staging database (`eoxs_wiki_staging`)

Purpose: a **complete physical mirror**, used exclusively as a safety net for testing raw-ingestion changes (new fetchers, schema tweaks, bug fixes) against realistic data with zero risk to the live database. Every raw-ingestion write goes through `dual_write()` (see `docs/raw-ingestion.md` §4), which writes to both databases in one call, live-mandatory / staging-best-effort.

Table structure is **exactly identical** to live (same 2 schemas, same extensions, zero table-list diff). Row counts largely match for raw tables (email_threads, tickets, call_transcripts, implementation_tasks, sales_orders all identical or within a few rows — normal in-flight lag), but several things are **deliberately never dual-written** to staging:

- `sync_cursors`, `message_ids_seen` — live-only bookkeeping (0 rows in staging)
- `wiki_ingest_cycles` / `wiki_ingest_batches` / `wiki_ingest_seen` — the wiki-ingestion pipeline never touches staging at all (0 rows)
- The entire `wiki_staging` schema — also live-only (0 rows in staging's copy)

**`access_tier` values differ meaningfully between live and staging** — staging still mostly shows the raw column *defaults* (e.g. `email_threads` in staging: 30,333 tier1 / 10 tier2_confidential / 41 tier2, vs. live's 1,449/5,753/23,182 split). This is because the tier-classification backfill (`ingestion/tier_classifier.py`) has only ever been run against live — this is expected current-state drift, not a dual-write bug (classification is a separate batch job, not part of the write path itself).

Database sizes (as of 2026-08-11): `eoxs_wiki` ≈ 525 MB, `eoxs_wiki_staging` ≈ 336 MB. No automated backup of either database exists yet — see the DigitalOcean migration roadmap and the infrastructure punch list for the plan to close this gap (either a scheduled `pg_dump` to object storage, or migrating to a managed Postgres service with automated backups built in).

## 9. `wiki_staging` — the schema, not to be confused with the database

Two genuinely distinct things share a similar name, and mixing them up is a common source of confusion:

- **`eoxs_wiki_staging`** (§8 above) — a separate physical *database*, used for raw-ingestion dual-write testing.
- **`wiki_staging`** (this section) — a *schema* living inside the live `eoxs_wiki` database, used as the draft-review workspace for AI-written wiki pages before a human promotes them to the real `public.wiki_pages` table.

They were deliberately designed to be same-database/different-schema (not a separate database) specifically so that **promotion is atomic** — a single transaction can move a page from draft to live, which wouldn't be possible if they lived in two separate physical databases.

Current real status counts in `wiki_staging.wiki_pages` (229 total): **4 draft**, **210 reviewed**, **13 rejected**, **2 promoted**. See `docs/wiki-ingestion.md` for exactly how a row moves through these statuses.

## 10. Views, functions, triggers

**None.** No views, no triggers, and no application-defined functions exist anywhere in the database — the only entries in `pg_proc` beyond Postgres's own catalog are `pg_trgm`'s built-in C functions (`similarity`, `gin_trgm_consistent`, etc.). Every piece of business logic (tier classification, wiki promotion, dedup) lives in Python application code, not in the database. If you're looking for "where does X get computed," the answer is never "a trigger" — it's always a specific `.py` file.

## 11. How to browse this database visually (for trusted people)

Two ways, both already set up:

1. **Web-based read-only browser** (pgweb) — reachable at `https://5.223.44.95/dbadmin/`, protected by an nginx basic-auth username/password. Nothing to install. Already connected as `eoxs_readonly`, so it's read-only end to end regardless of what you try to type into it.
2. **A full SQL client** (pgAdmin, DBeaver, etc.) via an SSH tunnel to the VPS — needs SSH access to `5.223.44.95` as user `deploy`, plus a Postgres username/password. Full querying power (read-write if connecting as `eoxs_app`, read-only if connecting as `eoxs_readonly`).

Contact **Ayan** for credentials for either path.
