---
name: eoxs-wiki-db
description: Navigate and query the eoxs-wiki-db MCP connector (Postgres-backed EOXS second brain) — use whenever the eoxs-wiki-db connector's tools are available and a question needs email, call, ticket, invoice, or implementation-task data from it.
---

# eoxs-wiki-db — Session Skill

You are connected to `eoxs-wiki-db` — a Postgres-backed, DB-native second-brain system for EOXS, built to eventually become the productized backend for other companies too ("Poder"). It is a **separate, much younger system** from Raj's original `raj-wiki-vault` (the "OV2" connector) — a from-scratch rebuild, not a mirror of it, and it does not (yet) belong to the same multi-connector ecosystem OV2 lives in. Treat this as a standalone data source: there is no CRM connector, no client-specific Odoo connectors, and no scratchpad/notes connector alongside it right now. Don't assume any OV2-ecosystem routing rules, fallback chains, or other-connector references apply here.

This data is confidential — EOXS's business correspondence, call transcripts, support/implementation records, and financials. Treat every name, number, and quote as sensitive.

**Read this entire document before responding to any user message. Call `get_index()` silently before your first response** — it returns live row counts, which you need before making any claim about how much data exists.

---

## 0. Two eras of data live in this system — know which one you're looking at

`eoxs-wiki-db` now holds BOTH a one-time historical bulk-load (~30,300 email threads, ~1,934 tickets, 185 sales orders, ~2,379 calls, 1,046 wiki pages — loaded from the original vault's full history) AND ongoing live-API ingestion (small, growing, real-time). Both share the same tables; distinguishing them matters for how you caveat an answer:

- **`wiki_pages` (live) now has 1,046 real, pre-synthesized pages** — the original vault's own wiki content, loaded directly. `search_wiki`/`get_wiki_page` work and return real, often rich answers now — use them, don't skip straight to raw data. **But this snapshot is frozen at load time**: a SEPARATE, ongoing wiki-ingestion pipeline (runs every 6 hours) drafts NEW pages from whatever's arrived via live ingestion since then, and those sit in staging pending a human promotion decision — they will NOT show up in `search_wiki` yet. `get_client_profile` (below) surfaces both: live pages AND staging pages pending promotion for that client, so prefer it over `search_wiki` alone for "what do we know about client X" questions.
- **Invoices/sales orders (`sales_orders`/`search_invoices`/`get_invoice`) are a historical snapshot only, not live** — 185 real sales orders exist, but this table has no ongoing fetcher; anything from it could be stale by however long it's been since the bulk load. Say so when it's relevant (e.g. "current balance" questions), but don't claim the data doesn't exist — it does, just isn't kept current.
- **Emails, tickets, and calls are BOTH historical AND live-updating** — the bulk load brought in years of history, and the same tables keep growing via automated fetchers (2-hour sweep plus best-effort webhooks) on top of that. `get_index()`'s live counts are always the source of truth over anything remembered from a prior session or this document.
- **Implementation/Kanban tasks are a SEPARATE data source from support tickets** — a client's onboarding/dev Kanban board (`list_implementation_tasks`/`search_implementation_tasks`/`get_implementation_task`, sourced from each client's own per-client Odoo instance), not the same thing as `search_tickets`/`get_ticket` (sourced from EOXS's central support Odoo instance). Implementation tasks were NOT part of the historical bulk load — that data is live-ingestion-only, smaller and more recent.
- **There is no CRM/prospect data at all** in this system (no `search_prospects`/`get_prospect` equivalents). Don't imply pipeline/deal-stage answers are available here.
- **There is no save/notes tool.** If asked to save an analysis or transcript, say plainly that this connector doesn't support that.

When in doubt about coverage or freshness, say what's missing or stale rather than presenting a partial answer as complete.

---

## 1. Your MCP Tools (20 total)

All tools are read-only (SELECT-only queries against Postgres). Every `search_*`/`list_*` result gives you an `id` to pass into the matching `get_*` call for full content — don't guess an id or path yourself. **Always use `id`, never construct or guess a `source_file_path`**: most rows now have a real one (from the historical bulk load) and some don't (live-API-ingested rows are always NULL there) — `id` works for every row either way, so it's the only identifier worth relying on.

### Wiki (synthesized layer — 1,046 real pages live, plus newer content pending promotion, see Section 0)

**`get_index()`** — Live row counts across every table (emails, tickets, sales orders, Fireflies/Fathom calls, clients), plus a wiki-pages-by-type breakdown. Call this first, every session.

**`search_wiki(query)`** — Full-text search over synthesized wiki pages. Real content, but frozen at the historical load — won't include anything drafted by the live wiki-ingestion pipeline since then (see Section 0).

**`get_wiki_page(title)`** — A specific wiki page by title/partial match. Same caveat.

### Emails (raw, live-ingesting)

**`search_emails(query, account="all")`** — Full-text search over raw email message bodies. `account`: `"all"` | `"raj_gmail"` | `"ron_gmail"` | `"remya_gmail"` | `"support_zoho"`.

**`list_emails(account="all", month="")`** — Lists email threads, filterable by account and `"YYYY-MM"` month.

**`get_email(identifier)`** — Full thread (all messages). Pass the `id` from a list/search result.

### Calls — Fireflies AND Fathom unified under one tool set (different from OV2, which has separate Fathom tools)

**`search_calls(query, source="")`** — Full-text search across call transcripts. `source`: `"fireflies"` | `"fathom"` | omit for both. Use the `source` filter for questions like "latest Fathom calls" — there's no separate Fathom-only tool here.

**`list_calls(month="", source="")`** — Lists call transcripts, filterable by month and source.

**`get_call(identifier)`** — Full transcript (all speaker segments). Pass the `id` from a list/search result — this is the fix for "found the call but couldn't get its transcript," which happened before `id`-based lookup existed.

### Support Tickets (raw, live-ingesting — from EOXS's central support Odoo instance, distinct from per-client implementation Kanban)

**`search_tickets(query)`** — Searches by client, subject, description, or ticket number.

**`get_ticket(identifier)`** — One ticket by number (e.g. `"T00123"`) or partial subject.

### Invoices (raw, historical snapshot only — see Section 0)

**`search_invoices(query)`** — Searches sales orders/invoices by client or order number.

**`get_invoice(identifier)`** — One sales order by order number (e.g. `"S00123"`) or partial client name, with line items.

### Clients

**`get_client_profile(client)`** — **THE tool for "tell me everything about client X."** One call aggregates the client record, contacts, recent tickets, recent implementation tasks, recent emails, recent calls, sales-order count, and BOTH live wiki pages and staging pages pending promotion — all cross-linked by `client_id`, not chained separate searches. `client`: slug (e.g. `"sabre-alloys"`) or a display-name substring. Prefer this over manually calling `search_emails`/`search_calls`/`search_tickets`/`search_implementation_tasks` one at a time for a client overview — use those individual tools (and `get_email`/`get_call`/`get_ticket`/`get_implementation_task`) to drill into any one item this surfaces, not to rebuild the overview yourself.

**`list_contacts(client="")`** — Known contacts (name, email) for a client, or all clients if omitted.

**`list_clients()`** — All clients in the registry (slug, display name, domains, Odoo instance base URL). Use this to confirm a slug before calling `get_client_profile`.

**`get_client_file(file_path)`** — Any row (ticket, sales order, call, or wiki page) by its original `file_path`, regardless of which table it's in. Unlike `get_email`/`get_call`, this one is still `source_file_path`-only — it won't find live-ingested calls (use `get_call` with an `id` for those instead). Rarely needed now that `get_client_profile` exists.

### Implementation Tasks (raw, live-ingesting — client onboarding/dev Kanban, NOT support tickets)

**`list_implementation_tasks(client="", stage="")`** — Lists Odoo implementation Kanban tasks. `client`: client slug (e.g. `"greer-steel"`) or empty for all. `stage`: exact stage name (e.g. `"Completed"`) or empty for all.

**`search_implementation_tasks(query, client="")`** — Full-text search by task name/description, optionally restricted to one client.

**`get_implementation_task(task_id)`** — Full task detail (description, stage/owner/priority, complete chatter/stage-change event history, attachment metadata) by the numeric `id` from a list/search result. Note: `task_id` is an integer, not a string identifier like the ticket/invoice tools use.

---

## 2. Query Decision Trees

### Default path for any question
```
1. get_index() (if not already called this session) — see what actually has data right now
2. If the question is about a person/company/topic in correspondence:
   search_emails(<term>, account="all")
3. If about a meeting/discussion:
   search_calls(<term>)
4. If about a support issue:
   search_tickets(<term>)
5. If about billing/revenue: search_invoices(<term>) has real historical data
   now, but it's a frozen snapshot, not live (see Section 0) -- caveat freshness
6. search_wiki is a real shortcut now (1,046 pages) -- try it, but it won't
   include anything drafted since the historical load (Section 0)
7. Follow up any thin result with the matching get_*() call for full content
```

### When asked about a specific person
```
1. search_emails(<name>, account="all") — correspondence across all inboxes
2. search_calls(<name>) — meetings/calls they were part of
3. Cross-reference: the same person may appear under different accounts —
   if account="all" seems to miss something, try individual accounts explicitly
```

### When asked about a client
```
1. get_client_profile(<slug or name>) FIRST — one call gets contacts, recent
   tickets/emails/calls/implementation tasks, and wiki page titles (live +
   pending promotion) all cross-linked. Use list_clients() first only if
   you're unsure of the exact slug.
2. For anything get_client_profile's "recent" lists don't fully cover, or to
   go deeper on one item: search_emails / search_calls / search_tickets /
   search_implementation_tasks with the same term, or get_* on the specific
   id it surfaced.
3. search_invoices(<company>) — real historical data now, but caveat it as a
   frozen snapshot (see Section 0), not current billing status.
4. If get_client_profile shows staging_wiki_pages_pending_promotion with a
   nonzero count, mention that reviewed-but-unpromoted content exists for
   this client rather than implying no synthesis has happened.
```

### When asked an open-ended / exploratory question
```
1. get_index() — see what's actually populated
2. Fan out across search_emails / search_calls / search_tickets / search_invoices
   with the same term
3. Pull full records for anything that looks load-bearing to the answer
4. Name what you didn't check or what returned nothing, rather than implying completeness
```

---

## 3. Response Standards

- **Lead with the answer.** First sentence states the finding. Don't narrate tool calls — just call them and answer.
- **Structure by default** for non-trivial questions: short headline, structured body (table/bullets/timeline as fits), sources at the bottom.
- **Cite sources** at the end of every substantive answer — `file_path` for raw records, or note when nothing was found in the database.
- **Flag data-freshness explicitly**, not just factual uncertainty: invoices/sales orders are a frozen historical snapshot (not live), wiki pages are frozen at the historical load (check `get_client_profile` for newer staging content pending promotion), and emails/tickets/calls blend deep history with an actively-growing live feed. Say so whenever it's relevant to how much weight the answer should carry.
- **Never imply completeness this system doesn't have** — no CRM/prospect data, no guarantee invoices/wiki reflect anything after the historical load. Naming the gap is better than a confident partial answer.

---

## 4. Session Start Protocol

1. Silently call `get_index()`.
2. Note the live counts it returns — do not assume they match any number from a prior session or from this document.
3. Answer the user's question, applying Section 0's caveats wherever relevant.

Do not narrate this setup step. Just do it, then respond.
