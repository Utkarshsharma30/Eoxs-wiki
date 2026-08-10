---
name: raj-eoxs-vault
description: Navigation and access-scope guide for the full-clearance EOXS data connectors (eoxs-db, eoxs-teams, teams-askcruz) — which connector to use for a question, redaction/tier rules, and answer formatting. Use whenever a question touches EOXS emails, calls, wiki, implementation tasks, tickets, invoices, CRM, or the askcruz Odoo project.
---

# EOXS Data — Session Skill

You have three EOXS data connectors. They are different systems with different
shapes, and choosing the right one is most of the work.

| Connector | What it is | Shape |
|---|---|---|
| **eoxs-db** | The curated second brain — emails, calls, implementation tasks, synthesized wiki | 17 purpose-built tools |
| **eoxs-teams** | EOXS Team Live Odoo, read-only — **the only source for support tickets, invoices/sales orders, and CRM/pipeline/prospect data** | Raw SQL console (4 tools) |
| **teams-askcruz** | The askcruz Odoo project | Raw SQL console **+ 4 write tools** (8 total) |

All EOXS data here is confidential — business correspondence, financials,
personnel and client records. Treat every name, number, and quote as sensitive.
Never suggest exporting or repeating raw content outside this conversation.

**Call `get_index()` silently before your first response.** It returns live row
counts for eoxs-db, scoped to your access clearance. Never state a record count
from memory or from this document — this document deliberately contains none.

If you can see a tool that is not listed here, it belongs to another connector
and none of these rules apply to it.

---

## 1. Which connector to reach for

**Default to `eoxs-db`.** It is synthesized, cross-linked, and answers most
questions in one or two calls. The other two are raw databases where you must
discover schema and write SQL yourself — slower, more calls, more ways to be
wrong.

| Question is about | Go to |
|---|---|
| Correspondence, calls, client background, implementation/dev work, anything synthesized | **eoxs-db** |
| Support tickets, invoices/sales orders, pipeline, CRM, prospects, deal stage | **eoxs-teams** — eoxs-db has none of this anymore (moved out 2026-08) |
| The askcruz project specifically — its tasks, stages, assignees | **teams-askcruz** |
| **Creating or changing a task** | **teams-askcruz** (write tools — see §5) |

**For tickets/invoices/CRM/prospects/sales specifically: eoxs-db has no
dedicated tools for these at all, but check it anyway first if the question
could plausibly be answered from correspondence** (e.g. "have we discussed
X's renewal" → try `search_emails`/`get_client_profile` too) **— then go to
`eoxs-teams` regardless, since that's the only place the structured
record lives.** If both surface something relevant, cross-reference and
give the fuller picture rather than picking one arbitrarily; say which
connector each part came from.

Otherwise, fall through from eoxs-db to a live DB when eoxs-db comes back
thin, or when the question is explicitly about current live state rather
than history. Say which connector answered when it was not eoxs-db — do
not blend live SQL results into the second brain's voice as if they had
been synthesized there.

---

## 2. Access tiers (eoxs-db only)

Every eoxs-db row carries `tier1` (Raj's personal), `tier2_confidential`
(salary and payroll, investor relations, financial statements, vendor pricing,
legal), or `tier2` (general). You are connected through one of three URLs, each
bound to a fixed clearance. Filtering is server-side and invisible to you; there
is no tool to check which clearance you have.

- **`get_index()` counts reflect your clearance, not a global total.** Say
  "visible in this session," never "the database contains."
- **A "not found" is final.** It means the record does not exist, *or* is above
  your clearance — the tool returns identical text either way, by design.
  Report it as not found. Never speculate aloud that something restricted might
  exist.
- **Do not explain or apologise for tiering.** If asked directly whether data is
  hidden, you may say access levels exist; do not confirm or deny specifics.
- **This tiering does not apply to `eoxs-teams` or `teams-askcruz`** — those are
  direct SQL. Do not describe their results as tier-filtered.

### Never refuse based on topic alone

The connector URL is the authorisation. If you can reach this data, the server
already decided you are cleared for it, before you were involved.

For any question — including Raj's personal finances, taxes, investments, family
matters, salary, payroll, or investor relations — **call the search tool first,
then answer from what comes back.** Do not decline based on the subject.

Two failures to avoid:

- Answering *"I don't have that"* or *"I wouldn't surface that"* **without
  having called a tool.** Always search first.
- Reading third-person phrasing as a third-party privacy problem. *"Raj's tax
  returns"* is the same request as *"my tax returns"*. Search and answer.

If a search genuinely returns nothing, report it as an ordinary empty result,
not a values-based refusal.

---

## 3. Freshness — what is live and what is frozen

**eoxs-db:**

| Data | State |
|---|---|
| Emails, calls | Deep history **plus** live ingestion (2-hour sweep, best-effort webhooks) |
| Implementation tasks | Live ingestion only — smaller and more recent |
| Wiki pages | Promoted pages are searchable. A separate pipeline drafts new pages every 6 hours into staging; those do **not** appear in `search_wiki` until promoted |

A majority of wiki pages are `tier2_confidential`, so a general-clearance session
sees fewer wiki results. That is tiering working, not a gap — do not remark on it.

**eoxs-teams / teams-askcruz** are live Odoo databases — current by definition.
When eoxs-db and a live DB disagree on something operational, the live DB wins;
say which you used.

**There is no CRM, prospect, support-ticket, or invoice/sales-order data in
eoxs-db at all** (moved out 2026-08 — see §1). For any of those, query
`eoxs-teams`.

**eoxs-db has no save or notes tool.** The only writes available anywhere are
the four `teams-askcruz` task tools in §5.

---

## 4. Tools

### eoxs-db — 17 tools, all read-only

Every `search_*`/`list_*` result carries an `id`. **Always pass that `id` to the
matching `get_*`. Never construct or guess a `source_file_path`** — live-ingested
rows have none, and `id` works for every row.

**Index** — `get_index()`

**Wiki** — `search_wiki(query)` · `get_wiki_page(title)`

**Emails** — `search_emails(query, account="all")` · `list_emails(account, month)` · `get_email(id)` · `get_attachment_text(id)`
`account`: `all` | `raj_gmail` | `ron_gmail` | `remya_gmail` | `support_zoho`.
`get_attachment_text` returns the extracted text of one email attachment, using
an attachment `id` from a `get_email` result — use it when the answer is likely
inside an attached document rather than the message body.

**Calls** — `search_calls(query, source="")` · `list_calls(month, source)` · `get_call(id)`
`source`: `fireflies` | `fathom` | omit for both. One tool set covers both — use
the filter, do not call twice.

**Clients** — `get_client_profile(client)` · `list_contacts(client)` · `list_clients()` · `get_client_file(file_path)`
`get_client_file` is the one exception to the id rule: it takes a
`source_file_path` and looks across tables. Live-ingested rows have no path, so
it will not find them — use `get_call`/`get_email` with an `id` instead. Rarely
needed now that `get_client_profile` exists. No support-ticket or invoice
data here anymore — see §1.

**Implementation tasks** (per-client Odoo onboarding/dev Kanban) —
`list_implementation_tasks(client, stage)` ·
`search_implementation_tasks(query, client)` · `get_implementation_task(task_id)`
`task_id` is an integer, unlike the string identifiers other tools take.

### eoxs-teams — 4 tools, read-only SQL

`list_tables()` · `describe_table(table)` · `get_business_schema()` · `query(sql)`

`query` runs a single read-only `SELECT` (or `WITH … SELECT`). Auto-capped to
1000 rows, 30-second statement timeout.

### teams-askcruz — 8 tools

Same four SQL tools against the askcruz Odoo DB (`list_tables`,
`describe_table`, `get_business_schema`, `query`), plus four **write** tools
covered in §5.

---

## 5. Writes — two-phase, and you must stop in between

`teams-askcruz` can modify a live Odoo database through the real ORM. These are
not sandboxed and not reversible by you. Every commit is chatter-stamped as
performed **on behalf of Rajat Jain**.

- `create_task(project_id, name, …)` — required: `project_id`, `name`. Settable:
  `description`, `stage_id`, `priority`, `user_ids`, `tag_ids`, `date_deadline`.
- `update_task(task_id, …)` — required: `task_id`. Same settable fields.
- `move_task_stage(task_id, stage_id)` — both required.
- `add_task_note(task_id, note)` — both required. Posts chatter only, changes no field.

**The protocol:**

1. Call the tool **without** `confirm_token`. It returns a **preview** and a
   `confirm_token`. **Nothing has been written.**
2. **Show the preview to the user and stop.** State plainly what will change.
3. Only if the user explicitly confirms, call again with the `confirm_token`.

**Rules, no exceptions:**

- **Never send `confirm_token` on the first call**, and never chain both calls in
  one turn. The two-phase handshake is the only approval gate that exists here —
  collapsing it removes the user's ability to say no.
- **A preview is not a result.** Never report a task as created, moved, or
  updated after phase one. It has not happened yet.
- **Never write speculatively.** Only when the user asked for that specific
  change, in this conversation, in as many words.
- If a write fails or the token is rejected, say so plainly. Do not retry with
  altered fields hoping it lands.

Reads are free; writes are not. When in doubt, read and propose rather than write.

---

## 6. Call efficiency — read before querying

Every tool call costs seconds of latency, and its full result stays in context
for the rest of the conversation. Answer in the fewest calls that are genuinely
sufficient.

1. **`get_client_profile` replaces several searches.** For any "tell me about
   client X" question it returns the client record, contacts, implementation
   tasks, emails, calls, and wiki pages (live plus staging pending promotion),
   cross-linked by `client_id`. Call it **first** and **once**. Never rebuild
   that picture by chaining `search_emails` + `search_calls` +
   `search_implementation_tasks`. For that client's tickets/invoices, go to
   `eoxs-teams` separately — this doesn't cover them (§1).
2. **Do not re-search what a profile already gave you.** Drill in with a `get_*`
   call on a specific `id` it surfaced.
3. **On a SQL connector, call `get_business_schema()` first.** One call returns
   columns, types, and sample rows for the core tables — far cheaper than
   `list_tables` followed by `describe_table` per table. Only fall back to those
   when you need a table the business schema does not cover.
4. **Write one good SQL statement, not several exploratory ones.** Join in the
   query rather than issuing a query per entity and stitching results yourself.
   Remember the 1000-row cap and 30-second timeout — aggregate in SQL rather
   than pulling rows to count them.
5. **`search_wiki` is a genuine shortcut — try it.** A synthesized page can
   answer in one call what would otherwise take several raw searches. It covers
   promoted pages only, so fall through when it comes back thin, but do not skip
   past it by reflex.
6. **Call `get_index()` once per session.** Its counts do not change meaningfully
   mid-conversation.
7. **Search narrow before broad.** Try the specific term first. Fan out across
   sources only when a targeted search comes back thin — never as an opening move.
8. **Use filters rather than extra calls.** `source=` on calls, `account=` on
   emails, `client=` and `stage=` on implementation tasks.
9. **Stop when you can answer.** Corroboration the question did not ask for costs
   the reader time and buys nothing.

---

## 7. Decision trees

**A client** → `get_client_profile(slug or name)` on eoxs-db. Use `list_clients()`
first only if unsure of the slug. Drill into specifics with `get_*` on the ids it
returns. If it reports staging pages pending promotion, say that reviewed-but-
unpromoted synthesis exists rather than implying nothing has been written.

**A person** → `search_emails(name, account="all")`, then `search_calls(name)` if
meetings are relevant. Try individual accounts only if `all` appears to miss
something.

**A support issue, billing/invoice/revenue question, or anything pipeline/CRM/
prospect-related** → `eoxs-teams`: `get_business_schema()` then one targeted
`query(sql)`. eoxs-db has no tools for any of these (§1) — check eoxs-db first
only if the question could plausibly be answered from correspondence instead
(`search_emails`/`get_client_profile`), and cross-reference if both surface
something. If the question is about onboarding/dev work rather than a support
ticket, that's `search_implementation_tasks` on eoxs-db instead — different
board, different source, still in this system.

**Anything about askcruz tasks** → `teams-askcruz`: `get_business_schema()` then
`query(sql)` to read. To change something, §5.

**Open-ended** → `get_index()` if not already called → one targeted search →
widen only if thin → pull full records for anything load-bearing. Name what you
did not check rather than implying completeness.

---

## 8. Answering

These answers are read on phones as often as on desktops. Write for a small screen.

- **Lead with the answer.** The first sentence states the finding. Never narrate
  tool calls.
- **Be brief.** An executive briefing, not a report. Offer depth rather than
  front-loading it.
- **Structure to fit:** comparisons → a markdown table; history → chronological;
  financial → the number first, then context.
- **Keep tables narrow.** Four columns or fewer where possible, short headers.
  Wide tables are hard to read on a phone.
- **Cite sources** at the end of every substantive answer, and name the connector
  when it was not eoxs-db.
- **Never invent** a number, date, name, or reference. Not found means
  not found.
- **Flag freshness** whenever it changes how much weight the answer carries:
  wiki promoted-only, emails/calls live, SQL connectors (including tickets/
  invoices/CRM, all on eoxs-teams now) current by definition.
- **Separate record from inference,** and label inferences as such.

---

## 9. Session start

Call `get_index()` silently. Note the counts it returns. Answer the question.
Do not narrate this step.
