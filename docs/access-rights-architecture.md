# Access Rights Architecture — Cruz

This document covers **every access-control mechanism across the whole
system** — two genuinely separate implementations, serving different data
with different risk profiles, deliberately not unified:

1. **`eoxs-wiki-db`'s tiered MCP + redaction system** — protects the main
   second-brain (emails, calls, wiki, tickets, employee records, internal
   documents). Content-level: a caller can see a *redacted version* of
   something above their clearance is nowhere close to.
2. **`eoxs-frontend-threads`'s department-based access-tier system** (built
   2026-08-18) — protects the personal/departmental chat-thread archive.
   Access-level, not content-level: a caller either sees a whole thread or
   nothing — there's no redaction, no partial visibility.

| | eoxs-wiki-db | eoxs-frontend-threads |
|---|---|---|
| What it protects | Company second-brain (emails, calls, wiki, tickets, employees, docs) | Personal/departmental saved chat threads |
| Enforcement style | SQL filter + LLM redaction safety net | Pure access boundary, no content filtering |
| Tiers | `tier1` / `tier2_confidential` / `tier2` (fixed 3-level scheme) | `tier1` (Raj, personal-exclusive) / department name (open-ended) |
| Classification | LLM agent, runs on every new row | Static lookup table — no LLM, department is a known fact |
| Identities | 5 fixed roles (`full`/`hr`/`general`/`intern`/`staging_qa`) | 1 per individual employee |
| Audit log | `mcp_redaction_log` (only actual redaction events) | `save_failures` (only failed/refused writes) |

---

# Part 1 — eoxs-wiki-db: Tiered MCP + Redaction System

## 1.1 What it's for

`eoxs-wiki-db` is one database serving multiple audiences with different
clearance — Raj personally, HR, general staff, interns — through **one set
of MCP tools**, without maintaining separate databases or separate code
per audience. The same `get_wiki_page` call returns different amounts of
content depending on which secret URL made the call.

## 1.2 The tier model

Every tiered row carries an `access_tier` **Postgres ENUM** column, three
values, in this ordinal order (added incrementally, so ordinal position
does *not* imply restrictiveness order — don't rely on `ORDER BY
access_tier`):

```sql
CREATE TYPE access_tier AS ENUM ('tier1', 'tier2', 'tier2_confidential');
-- tier2_confidential was inserted via
--   ALTER TYPE access_tier ADD VALUE 'tier2_confidential' AFTER 'tier1';
-- so it sorts between tier1 and tier2, not after both.
```

| Tier | Meaning | Who sees it |
|---|---|---|
| `tier1` | Raj's own personal data — personal finances, personal taxes, family/personal-life matters that are not company business | `full` only |
| `tier2_confidential` | Company-confidential business data — salary/payroll/compensation for **any** employee, investor relations, financial statements, vendor contracts, legal/compliance, employee activity/performance/productivity monitoring (e.g. Cattr) | `full`, `hr`, `general` |
| `tier2` | General — everything else | Everyone |

Tables carrying `access_tier`: `email_threads` (default `tier1`),
`call_transcripts` (default `tier1`), `wiki_pages` (default `tier1`),
`wiki_staging.wiki_pages` (default `tier1`), `tickets` (default `tier2`),
`sales_orders` (default `tier2`), `implementation_tasks` (default
`tier2`), `assets` (no default — must always be explicitly classified,
see §1.6). `employees` and `list_clients`/`list_contacts` data have **no**
`access_tier` column — they sit outside the tiered system entirely (see
§1.8).

## 1.3 The 5 identities

Each identity is a `(name, secret_env_var, clearance, extra_redact_categories, enable_employee_tools, asset_write_scope, database)` tuple, defined in `mcp_server/http_server.py`:

```python
FULL_CLEARANCE    = ["tier1", "tier2_confidential", "tier2"]
HR_CLEARANCE      = ["tier2_confidential", "tier2"]
GENERAL_CLEARANCE = ["tier2"]

IDENTITIES = [
    ("full", MCP_URL_SECRET, FULL_CLEARANCE, (), True, "all", None),
    ("hr", MCP_HR_URL_SECRET, HR_CLEARANCE, ("non_payroll_monetary_amounts",), True, {"eoxs-salary-details"}, None),
    ("general", MCP_GENERAL_URL_SECRET, HR_CLEARANCE, ("monetary_amounts", "employee_activity_monitoring"), False, None, None),
    ("intern", MCP_INTERN_URL_SECRET, GENERAL_CLEARANCE, ("monetary_amounts",), False, None, None),
    ("staging_qa", MCP_STAGING_URL_SECRET, FULL_CLEARANCE, (), True, "all", "staging"),
]
```

| Identity | Clearance | Extra content-based redaction | Employee tools | Asset writes | DB |
|---|---|---|---|---|---|
| `full` | tier1+tier2_confidential+tier2 | none | read+write | create+update, any doc | live |
| `hr` | tier2_confidential+tier2 | non-payroll $ stripped (payroll visible) | read+write | update only, salary doc only | live |
| `general` | tier2_confidential+tier2 | **every** $ stripped incl. payroll, + activity-monitoring data | none | none | live |
| `intern` | tier2 only | every $ stripped | none | none | live |
| `staging_qa` | tier1+tier2_confidential+tier2 | none | read+write, unrestricted | unrestricted | **staging** (`eoxs_wiki_staging`) |

`general` was widened 2026-08-11 from tier2-only to the same DB-level
clearance as `hr`, because most `tier2_confidential` pages carry one
dollar figure alongside otherwise-relevant general content — losing the
whole page over one number was worse than stripping just the number.

Tool counts: **23 read-only tools** for every identity (added
`list_repo_docs`/`search_repo_docs`/`get_repo_doc` 2026-08-26, tier1-only —
see `docs/backend-server.md` §5.5); `full`/`hr` also
get 7 employee tools + 1–2 asset-write tools (**32 for `full`, 31 for
`hr`**, since `hr` has no `create_asset`); `general`/`intern` stay at 23;
`staging_qa` sees all 32, every one hitting staging.

## 1.4 How identity is enforced — URL secret, not login

No OAuth, no auth headers. Each identity is mounted at its own path,
`/mcp/<secret>/sse` (GET, SSE) + `/mcp/<secret>/messages/` (POST), via a
separate `SseServerTransport` per identity in `http_server.py`. The long
random secret embedded in the URL **is** the credential — matches the
pattern already proven in the older `raj-wiki-vault` connector. nginx
passes `/mcp/` through completely unchanged (no prefix stripping),
because `SseServerTransport` bakes its own external path into the SSE
"endpoint" event it sends the client — stripping the prefix there would
make the SSE connect look successful while every follow-up POST 404s.

`build_server(clearance, name, extra_redact_categories, enable_employee_tools, identity_name, asset_write_scope, database)`
builds a **fresh `Server` instance per identity**, with every tool
function's `clearance` (and `changed_by`, `_allowed_slugs`, `database`)
bound via `functools.partial` **at construction time** — never present in
any tool's JSON `inputSchema`. Nothing a caller sends in a tool call can
widen its own access, spoof who made a write, or escape the staging
sandbox.

## 1.5 SQL-level tier filtering

The primary enforcement layer. Every tiered tool's query carries the same
pattern:

```sql
WHERE access_tier::text = ANY(%s)
```

with `clearance` (e.g. `["tier1","tier2_confidential","tier2"]`) as the
parameter, and every tiered tool function takes `clearance=FULL_CLEARANCE`
as a keyword default, overridden per-identity via the `functools.partial`
binding in §1.4.

20 of the 23 read tools are tier-filtered (`TIER_FILTERED_TOOLS` in
`server.py`): `get_index`, `get_wiki_page`, `search_wiki`, `list_emails`,
`search_emails`, `get_email`, `get_attachment_text`, `list_calls`,
`search_calls`, `get_call`, `list_assets`, `search_assets`, `get_asset`,
`get_client_profile`, `get_client_file`, `list_implementation_tasks`,
`search_implementation_tasks`, `get_implementation_task`,
`list_repo_docs`, `search_repo_docs`, `get_repo_doc`. The 3
exceptions — `list_clients`, `list_contacts` — have no `access_tier`
column on their underlying rows, so nothing to filter. A tool reaching a
child row only through a parent that already carries `access_tier` (e.g.
`get_attachment_text` on `email_attachments`) enforces via a join back to
the parent's tier, not its own column — "defense-in-depth pattern used
everywhere a child row is only ever reachable through an already-checked
parent."

## 1.6 The classification agent(s) — assigning a tier to NEW content

**`ingestion/inline_tier_classifier.py`** is the one that actually runs at
write time for new rows:

- Model: `claude-haiku-4-5-20251001`, capped at 2000 chars of context.
- `classify_tier(context)` — one API call, returns `tier1` /
  `tier2_confidential` / `tier2`, **never raises** — any exception
  defaults to `tier1` (the most restrictive), fail-closed.
- Runs **only on a row's first INSERT**, never re-evaluated on later
  updates to the same row — "a thread's tier is computed once and
  preserved, not re-evaluated as new messages get appended." Every
  `write_*.py` ingestion module excludes `access_tier` from its `ON
  CONFLICT DO UPDATE SET` clause to enforce this.
- Used directly by `mcp_server/asset_writes.py`'s `create_asset` tool and
  by `ingestion/import_assets.py`'s one-time backfill.

**Wiki pages are different — not independently LLM-classified at all.**
`wiki_ingestion/promote.py` computes a new page's tier deterministically,
at promotion time, as the **MAX (most restrictive) of every resolved
citation's own tier**:

```sql
-- across all 4 real raw source types a citation can point to
SELECT t.access_tier::text FROM wiki_citations wc JOIN email_threads t ON ...
UNION SELECT ... FROM call_transcripts ...
UNION SELECT ... FROM tickets ...
UNION SELECT ... FROM implementation_tasks ...
```
then `tier1` if any citation is `tier1`, else `tier2_confidential` if any
is that, else `tier2`. Guarantees "a synthesized page can never leak a
tier1 source by citing it from an otherwise-tier2 page" — reliable because
the drafting agent can only cite a real, resolved raw row, never an
"unresolved" placeholder.

**Two standalone batch classifiers** (`ingestion/tier_classifier.py` for
raw source tables, `wiki_ingestion/tier_classifier.py` for the ~1,046
pre-existing wiki pages migrated from the old vault) exist for historical
backlog cleanup during the 2-tier→3-tier redesign — not part of the
steady-state pipeline. The wiki-page version applies two backstop passes
afterward that only ever tighten, never loosen: upgrade to the most
restrictive cited tier, then propagate wiki-cites-wiki tier upgrades to a
fixed point.

**Convention, confirmed everywhere**: classify once at creation, never
reclassify on edit. `update_asset` explicitly does not touch
`access_tier`. Re-running `import_assets.py` (an upsert) *does*
reclassify — the one deliberate exception, since it's a manual backfill
tool, not a live write path.

## 1.7 The redaction safety net — the second, independent layer

`mcp_server/redaction.py`'s `check_and_redact()` runs on **every tool
call for every identity except `full`** (checked via `if "tier1" not in
clearance` in `call_tool()` — `full`'s clearance always contains `tier1`,
so it's the only identity that skips this entirely). It is *not* a
replacement for §1.5's SQL filter — everything it inspects has already
passed that filter. Its only job is to catch cases where the **original
classification was wrong**.

**Categories checked** — restricted tiers not in this identity's
clearance, plus per-identity `extra_redact_categories`:
- `tier1` / `tier2_confidential` — same definitions as §1.2.
- `monetary_amounts` — any currency amount, any unit, regardless of tier.
- `non_payroll_monetary_amounts` — any amount that is *not* clearly an
  employee's own payroll/salary/incentive/bonus figure (HR's carve-out).
- `employee_activity_monitoring` — Cattr/performance/productivity data,
  regardless of tier.

**Flow:**
1. If `monetary_amounts` is active, a deterministic pre-pass
   (`_strip_monetary_fields`) blanks known numeric field names
   (`amount_total`, `unit_price`, `subtotal`, etc.) *before* the LLM ever
   sees them — added after a real incident where a raw `Decimal` field
   passed through completely unredacted, since it's never a string until
   final JSON serialization.
2. Every string leaf in the result (`_walk_strings`, recursive over
   dicts/lists) is sent to **`claude-sonnet-5`** — deliberately a
   different, stronger model than the Haiku classifier that made the
   original tier call, "so this isn't just re-running the same judgment
   that may have already been wrong."
3. The model returns exact spans to redact. Each span is validated —
   discarded unless it actually occurs verbatim in the real content
   (never trust a possibly-hallucinated span).
4. Validated spans are removed via **direct string substitution on the
   real Python structure** (`_replace_in_structure`), never by asking the
   model to regenerate text, and never by comparing against a
   `json.dumps()`-serialized blob.
5. Up to 3 retries against the Anthropic API on failure. If all 3 fail,
   returns `_BLOCKED_RESULT = {"error": "This information is temporarily
   unavailable. Please try again."}` — fails closed, and deliberately
   worded to look like an ordinary transient failure, not a hint that
   something is being hidden.
6. Any actual redaction (not a clean check) is logged (§1.10).

**Why direct string substitution, not `json.dumps()` comparison — a real
incident (2026-08):** the first version compared against a JSON-serialized
blob. `json.dumps()` escapes an embedded `"` to `\"` and non-ASCII
characters (e.g. ₹) to `\uXXXX` by default — the model naturally quotes
the span back *unescaped*, so the substring match silently found nothing,
and a real confidential incentive figure reached a general-clearance user
untouched. Walking actual string field values instead of a serialized
form sidesteps this whole class of bug structurally.

## 1.8 Write scopes — exactly two surfaces, nowhere else

By explicit instruction, no other table has or will get a write path.

**Employees** (`mcp_server/employees.py`, 2026-08-12 — this server's
first-ever write tools): `list_employees`/`search_employees`/`get_employee`
(read) + `create_employee`/`update_employee`/`deactivate_employee`/
`reactivate_employee` (write), gated to `full` and `hr` only via
`enable_employee_tools`, independent of `clearance` (so `general`, which
shares `HR_CLEARANCE` with `hr` for everything else, still gets none of
this). `employees` has no `access_tier` column — sits entirely outside
the tiered-content system. Soft-delete only (`deactivate_employee` sets
`status='inactive'`, never a real `DELETE`). Every write logged to
`employee_change_log` with `changed_by` bound to the identity name at
server-construction time.

**Assets** (`mcp_server/asset_writes.py`, 2026-08-13 — the second and,
per explicit instruction, *last* write surface): `create_asset`/
`update_asset` for the `assets` table. `full` gets both, unrestricted;
`hr` gets `update_asset` **only**, and only for slug
`eoxs-salary-details` — any other slug returns a plain permission error,
not a partial write. `general`/`intern` get neither. `create_asset`
auto-classifies `access_tier` via the inline classifier (§1.6), never a
caller-supplied argument. `update_asset` never reclassifies. Every write
logged to `asset_change_log` with full old/new title+body text (not just
field diffs, since assets are "a handful of long documents").

## 1.9 `staging_qa` — structurally isolated QA sandbox

Full clearance, every tool unrestricted (nothing to protect — disposable
test data) — but every single call, read or write, is transparently
routed to the **`eoxs_wiki_staging` database** instead of live
`eoxs_wiki`, via a `contextvars.ContextVar` (`mcp_server/db.py`'s
`use_database()`) that `build_server()`'s `database` param sets for the
call's duration. Required editing only `db.py` and one wrapper line in
`call_tool()`.

**Structural, not just policy, isolation**: `wiki_ingestion/` has zero
code path to `eoxs_wiki_staging` at all (confirmed by grep — no
`get_staging_conn`/`PGDATABASE_STAGING` references anywhere in that
package), so a write through this identity cannot reach the wiki pipeline
regardless of intent. (Not to be confused with the *`wiki_staging`
schema* inside live `eoxs_wiki` — the draft-review workspace pages sit in
before promotion. Two unrelated things sharing a similar name.)

Every write tagged `changed_by='staging_qa'`.
`loaders/reset_staging_qa_data.py --commit` wipes staging's
`employees`/`assets`/both change-logs and re-mirrors from live for a
clean slate between sessions. Deliberately not documented in any of the 4
main skill files — see `deploy/eoxs-wiki-db-skill-staging-qa.md`.

## 1.10 Audit logging

**`mcp_redaction_log`** — records only actual redaction *events* (a clean
check writes nothing):

```sql
CREATE TABLE mcp_redaction_log (
    id                  SERIAL PRIMARY KEY,
    occurred_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    clearance_name      TEXT NOT NULL,   -- 'hr' | 'general' -- never 'full'
    tool_name           TEXT NOT NULL,
    redacted_snippets   TEXT[] NOT NULL  -- exact spans removed
);
```
335 rows as of the last documented count. **View it via pgweb** at
`https://5.223.44.95/dbadmin/` — or `https://68-183-181-25.nip.io/dbadmin/` on the
DigitalOcean box (basic auth — same credentials as the rest
of the admin surface), table `mcp_redaction_log` — or `psql`/any SQL
client against live `eoxs_wiki`. A row here means the *original*
classification was wrong about that specific content — the table exists
to make that visible for fixing the source, not to be relied on forever
as a catch-all.

**`employee_change_log`** / **`asset_change_log`** — every write to
either surface, `changed_by`, `change_type`, and a JSON diff (or full
old/new text, for assets) of what changed.

## 1.11 "Not found" is deliberately indistinguishable from "restricted"

A lower-clearance caller gets the exact same plain "not found" text
whether a record genuinely doesn't exist or exists but sits above their
clearance — by design, so trial-and-error can never confirm something
restricted exists. `redaction.py`'s `_BLOCKED_RESULT` on exhausted
retries follows the same principle: worded like an ordinary transient
failure, never a hint that something is being hidden.

## 1.12 The `_environment` field — real incident, real fix (2026-08-14)

**The incident**: a `staging_qa` session with no skill file attached had
no factual basis anywhere — not in any tool result, only in skill-file
text — to know it was in the sandbox. It nearly asked the user to approve
a write it believed was live. Nothing was actually written anywhere in
that session (verified against both databases directly afterward), but
the model's *belief* was wrong, purely because the "which environment"
signal lived entirely out-of-band in prompt text a session could be
missing.

**The fix**: every result from a write tool (all 4 employee + both asset
tools, every identity, success or error alike) gets a server-asserted
`_environment` field, stamped in `call_tool()` **after** redaction (so
it's never at risk of being touched by that step):

```python
result["_environment"] = (
    "STAGING (eoxs_wiki_staging via the '<identity>' identity) -- test data..."
    if database == "staging" else
    "LIVE (eoxs_wiki via the '<identity>' identity) -- this write is real..."
)
```
Built from the same `database`/`identity_name` values bound at
construction time — never something a caller or a missing skill file can
spoof or omit. Deliberately scoped to write tools only (20+ read tools
don't have this problem). All 3 write-capable skill files now explicitly
tell the model to trust this field over its own assumption.

## 1.13 A known, documented gap

`eoxs_readonly` (a genuinely read-only Postgres role, used by pgweb) does
exist, but the MCP server does **not** connect as it — `mcp_server/db.py`
uses the same full-read-write `eoxs_app` credentials as every writer. The
"read-only" guarantee for the 20 read tools today is a **code-discipline
convention**, not a database-enforced one.

---

# Part 2 — eoxs-frontend-threads: Department-Based Access Tiers

## 2.1 What it's for

`eoxs-frontend-threads` archives every employee's Claude conversation (via
`save_chat_transcript`, called automatically by the model — see
`docs`/session history for the reliability work around that). This
access-tier layer (added 2026-08-18) controls **who can read whose saved
threads**:

- **Raj**: his threads are personal and exclusive — no one else, ever.
- **Everyone else**: threads are shared **read-only within their own
  department** — a Sales employee can read another Sales employee's
  threads, but not Support's, and vice versa.
- **Writes are never shared, for anyone** — you can only ever create or
  append to your *own* threads.

Purpose: cross-referencing tribal/informal knowledge within a team,
without one department's conversations leaking into another's, and
without Raj's personal archive being visible to anyone else at all.

## 2.2 The tier model

Simpler and open-ended compared to eoxs-wiki-db's fixed 3-level scheme —
a plain text value, either:
- `'tier1'` — Raj, personal, exclusive. Mirrors `eoxs-wiki-db`'s own
  `tier1` = "Raj personal" concept directly (same person, same idea,
  applied here) — nobody else is ever assigned this value.
- any department name (`'sales'`, `'support'`, etc.) — shared read access
  among everyone with that same value.

```sql
CREATE TABLE user_access_tiers (
    username     TEXT PRIMARY KEY,
    access_tier  TEXT NOT NULL,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE threads ADD COLUMN access_tier TEXT;
CREATE INDEX idx_threads_access_tier ON threads(access_tier);
```

## 2.3 No classification agent, by deliberate design

Unlike eoxs-wiki-db's LLM classifier, department membership here is a
**known, static fact** provided directly (a username→department list),
not something to infer from content. An LLM classifier would add cost,
latency, and a real misclassification failure mode for something that's
actually just a lookup. `user_access_tiers` is a plain table, populated
directly — no agent, no prompt, no model call anywhere in this path.

## 2.4 How a thread gets its tier — once, at creation, never reclassified

Exactly mirrors `eoxs-wiki-db`'s own convention (§1.6): `access_tier` is
resolved from the creator's `user_access_tiers` row and stamped **only on
first creation** of a thread. The upsert always looks up and passes the
current tier, but Postgres only actually *applies* it on `INSERT` — the
`ON CONFLICT ... DO UPDATE SET updated_at = now()` clause never touches
the column, so an existing thread's tier survives every later append
untouched, automatically, with no special-case code needed:

```sql
INSERT INTO threads (username, thread_name, access_tier)
VALUES (%s, %s, %s)
ON CONFLICT (username, thread_name) DO UPDATE SET updated_at = now()
RETURNING id
```

**Fails closed**: a caller with no row in `user_access_tiers` is refused
outright — cannot create *or* append to any thread — rather than
guessing a tier. The refusal is logged to `save_failures` for admin
visibility.

## 2.5 Write enforcement — structural, no separate check needed

Identity (`username`) is bound to a specific connector mount at server
construction time (§2.7) — never an argument in any tool call. There is
therefore no code path by which one user's connection could write into
another user's thread, regardless of department or tier. This required no
new enforcement logic when the tier feature was added — it was already
true by construction, the same way `eoxs-wiki-db`'s `clearance` binding
means no caller argument can widen access there either.

## 2.6 Read enforcement — the actual new logic

`list_threads()` resolves the caller's own tier, then queries
`WHERE access_tier = <that value>` instead of `WHERE username = <caller>`
— returning every thread in the caller's tier, not just their own, each
row tagged with who it belongs to. Because Raj is the only person ever
assigned `'tier1'`, this single query shape naturally gives him exactly
his own threads and gives everyone else their whole department — no
special-casing in the query, only in who gets assigned which tier.

`get_thread(thread_name, owner_username=None)` — omit `owner_username` to
read one of your own threads (default, unchanged from before this
feature existed). Pass it to read a specific department-mate's thread by
name. **Verified server-side on every call**, never trusted from the
argument alone:

```python
if owner != username:
    # look up both callers' tiers, refuse unless they match
    if username not in tiers or owner not in tiers or tiers[username] != tiers[owner]:
        return {"result": f"Not permitted to read {owner!r}'s threads (...)"}
```

## 2.7 Identity — per-individual secret, not per-role

Different from `eoxs-wiki-db`'s 5 fixed role-based secrets: each
**individual employee** gets their own secret, mapped 1:1 to a username.
Originally implemented via a shared FastMCP instance + an ASGI middleware
that scraped SSE handshake responses to map `session_id → username` in an
in-memory dict — rebuilt (2026-08-18, same day as the tier feature) to
mirror `eoxs-wiki-db/mcp_server/http_server.py`'s proven pattern instead:
**one `Server` + one `SseServerTransport` per secret**, mounted at its own
`/threads/<secret>/sse` + `/threads/<secret>/messages/` path. Every
request — the SSE connect *and* every subsequent message POST — carries
its secret directly in its own URL, so identity resolves statelessly via
routing, with nothing to lose on a service restart and no "unknown"
fallback possible. `MOUNT_PREFIX = "/threads"`, matching the nginx
`location /threads/` block (mirrors eoxs-wiki-db's own `/mcp/` block:
`proxy_buffering off`, `proxy_read_timeout 3600s`, no prefix stripping,
same reasoning as §1.4).

## 2.8 No redaction, no content filtering — by design

The single biggest structural difference from eoxs-wiki-db: this system
protects *access* to whole threads, not content *within* a thread. If
you're in the right tier, you see the full, unredacted saved conversation
— there is no per-field stripping, no monetary-amount redaction, no LLM
safety-net pass. That model doesn't fit this data (personal/departmental
conversation archives, not a company knowledge base with graduated
sensitivity levels within a single document).

## 2.9 Where this connects to the reliability work

Access tiers sit on top of the same `save_chat_transcript`/`get_thread`/
`list_threads` tools hardened earlier the same day: retries with backoff
on DB failure, content-based dedupe (a retried call with identical
content is skipped, not duplicated), and a `save_failures` audit table
(thread_name, username, error_text, occurred_at) — the same table an
unclassified-user refusal (§2.4) logs to. A dual-trigger autosave
instruction (save the *previous* exchange first, the *current* one last)
was added after two real observed misses in a long, multi-connector
conversation — see the skill file and server `instructions` text for the
current wording; that part is a reliability concern, not access control,
so it's only summarized here for context.

---

# Part 3 — Quick Reference

## Where to look things up

| Question | Where |
|---|---|
| eoxs-wiki-db tier definitions, identities | `mcp_server/server.py` (constants), `mcp_server/http_server.py` (`IDENTITIES`) |
| eoxs-wiki-db redaction logic | `mcp_server/redaction.py` |
| eoxs-wiki-db redaction audit log | `mcp_redaction_log` table, live `eoxs_wiki` — pgweb at `https://5.223.44.95/dbadmin/` (Hetzner) or `https://68-183-181-25.nip.io/dbadmin/` (DigitalOcean) |
| eoxs-wiki-db write audit logs | `employee_change_log`, `asset_change_log` tables, same pgweb |
| eoxs-wiki-db classification agent | `ingestion/inline_tier_classifier.py` (live, per-row); `wiki_ingestion/promote.py` (wiki pages, citation-MAX, not LLM) |
| eoxs-frontend-threads tier table | `user_access_tiers`, `eoxs_frontend_threads` DB |
| eoxs-frontend-threads thread tiers | `threads.access_tier` column, same DB |
| eoxs-frontend-threads write-failure audit | `save_failures` table, same DB |
| eoxs-frontend-threads identity binding | `mcp_server.py`'s `_make_routes()`, `FRONTEND_THREAD_USERS` env var |

## Cross-cutting principles both systems share

1. **Identity is always bound server-side at connection/construction
   time, never taken from a tool call's own arguments** — the one
   consistent rule underlying every access boundary in this document.
2. **Fail closed, not open** — an unknown/unclassified caller is refused,
   never defaulted to the most permissive option, in both systems.
3. **Classify once, never reclassify silently on edit** — both systems
   independently arrived at the same convention.
4. **Not-found and not-permitted read the same** wherever it matters —
   eoxs-wiki-db's tier boundary; eoxs-frontend-threads' cross-tier
   `get_thread` refusal.
