# Wiki-Ingestion Pipeline

*Deep technical reference for how Cruz (`eoxs-wiki-db`) turns raw ingested data into AI-synthesized wiki knowledge pages — for developers being onboarded to this subsystem.*

## 1. The `wiki_ingestion/` directory

```
wiki_ingestion/
  detect.py                        307  Phase 3a: find what changed since last cycle
  run_cycle.py                     254  Phase 3 driver: chunk + dispatch sub-agents
  run_agent.py                      72  Phase 3: prompt template + single sub-agent call
  headless_agent.py                122  claude -p subprocess wrapper (shared by all 3 phases)
  agent_mcp_server.py               259  Phase 3's sub-agent MCP tool server
  consolidate.py                     36  Phase 4a: find duplicate-title drafts
  run_consolidation.py               77  Phase 4 driver + prompt
  consolidate_mcp_server.py         159  Phase 4's sub-agent MCP tool server
  review.py                          28  Phase 5a: find all draft pages
  run_review.py                      93  Phase 5 driver + prompt (see the known bug, §6)
  review_mcp_server.py              143  Phase 5's sub-agent MCP tool server
  promote.py                        221  Promotion (now automatic post-review) + access_tier computation
  run_pipeline.py                    42  Top-level scheduled entrypoint (3 -> 4 -> 5)
  citation_resolver.py              160  Legacy citation repair, pattern-based (no LLM)
  citation_llm_resolver.py          334  Legacy citation repair, LLM-assisted, two-stage
  tier_classifier.py                262  Legacy-page access_tier classification (pre-promote.py pages)
  repair_stale_task_citations.py    127  One-off incident repair script
  staging_db.py                      29  Staging-schema DB connection helper
```
3,946 lines total, 21 files (recomputed 2026-08-28). The list above is not exhaustive as of this count — `rerun_assets_manual.py` and `mcp_http_server.py` have been added since it was last written; see `git log --stat -- wiki_ingestion/` for the exact current file set.

## 2. The phases, in order, exactly what each does

This pipeline is a strict sequential pipeline, not a loop of independent jobs. Phase numbers below match the actual module docstrings.

### Phase 3 — Detect & Draft

**Repo-docs sync (added 2026-08-28, runs before detection):** `run_pipeline.py` calls `ingestion.import_repo_docs.import_all()` first, every cycle, wrapped in try/except so a bad doc file can never block real ingestion. Previously this only ran when someone remembered the manual `python -m ingestion.import_repo_docs` command — meaning an edit to `docs/*.md`/`ARCHITECTURE.md` had genuinely no automatic path into the wiki. Re-running it here is safe (pure upsert; unchanged content produces no new `updated_at`) and is what gives the `repo_docs` partition below anything to detect.

**Detection** (`detect.py`, `run_detection(cycle_id)`): for each of the email accounts (`raj_gmail`, `ron_gmail`, `remya_gmail`, `support_zoho`), plus `tickets`, `calls`, `assets`, `repo_docs`, and one partition per client (`client_{slug}`):
1. Reads a cursor from `sync_cursors` under key `wiki_ingest_{source_kind}` — a **separate cursor namespace** from raw-ingestion's own cursors on the very same `sync_cursors` table.
2. Pulls candidate rows updated since that cursor.
3. Computes a SHA-256 content hash per row (including its full child-table content — email body, ticket description+events, transcript segments, task description+events).
4. Compares each hash against `wiki_ingest_seen` and drops unchanged rows, recording every decision (`processed`/`skipped_unchanged`) for full auditability — nothing is dropped silently.
5. Advances the cursor.

Implementation tasks use `odoo_task_id` as the stable identity instead of the serial `id`, because `odoo_fetcher.py` does a full delete+insert every raw-ingestion sweep, making `id` unstable across runs.

**Draft/ingest** (`run_cycle.py` + `run_agent.py`): the cycle driver inserts a `wiki_ingest_cycles` row, runs detection, then splits every partition's rows into chunks of `CHUNK_SIZE=25` (recorded as `wiki_ingest_batches` rows), and runs one `claude -p` sub-agent **per chunk, sequentially** — never concurrently (the box has 2 CPU cores, and sequential execution keeps failure attribution simple). Each sub-agent gets a prompt listing its chunk's rows and is instructed to search existing wiki pages first, pull full context via read tools (not draft from the summary alone), cite every source, cross-link related pages, and flag contradictions/unverified claims.

`resume_cycle(cycle_id)` handles interrupted cycles: reaps any batch still `running` (marks it `ingest_failed`), rebuilds the exact candidate set, and replays only chunks not already `done`.

### Phase 4 — Consolidate

**Detection** (`consolidate.py`) is a single SQL query:
```sql
SELECT min(title), array_agg(id ORDER BY id)
FROM wiki_staging.wiki_pages
WHERE status = 'draft'
GROUP BY lower(trim(title))
HAVING count(*) > 1
```
Deliberately **exact-title matching, not fuzzy** — verified empirically against real duplicates (51 of 217 pages from an early backfill), all of which turned out to be byte-identical titles. Fuzzy matching isn't built until an actual case demands it.

**Merge** (`run_consolidation.py`): one sub-agent per duplicate group, instructed to read every group member, synthesize one merged body (dedupe overlap, preserve genuinely distinct sub-topics, flag real contradictions instead of silently picking a version), and call `merge_staging_pages(keep_id, duplicate_ids, merged_body, ...)` exactly once — which reassigns citations/flags/links from the duplicates onto the kept page, overwrites its content, and deletes the duplicates, all in one transaction.

### Phase 5 — Review

**Detection** (`review.py`, `find_draft_pages()`): every `wiki_staging.wiki_pages` row with `status='draft'`, across **all** cycles ever (not scoped to the current run), chunked to `REVIEW_CHUNK_SIZE=15` (smaller than ingestion's 25, since review does more per-page tool calls).

**Review** (`run_review.py`): one sub-agent per chunk, instructed to read each page, spot-check at least one citation against the real raw source, judge quality, and call exactly one of `mark_reviewed`/`mark_rejected` for **every** page in the batch. See §6 for a known reliability gap here.

### Promotion — automatic immediately after every review sweep (2026-08 change)

`promote.py`'s `promote_page(staging_page_id)` requires `status='reviewed'`, then:
1. INSERTs into (or UPDATEs) the live `wiki_pages` table.
2. Fully replaces `wiki_citations`/`wiki_flags`/`wiki_links` from the draft's current set.
3. Computes `access_tier` (see §5).
4. Marks the staging row `promoted`, records `live_page_id`.

`promote_reviewed_pages()` runs this over every `reviewed` row, then re-resolves any wiki-links that pointed at a page promoted later in the same batch, then reports to Linear. **`run_review_sweep()` now calls this automatically at the end of every sweep** — there is no human gate for a page the review sub-agent marked `reviewed`; it goes live within the same cycle, typically well under a minute later. This was a deliberate change (no longer "manual, run by a human when they've decided to publish") — only a `rejected` page still requires a human decision (see `docs/linear-integration.md` §5/§9 for how those two outcomes are now tracked as distinct, filterable Linear states). Live confirmation: `wiki_staging.wiki_pages` currently has **zero** rows sitting in `reviewed` status — every page that clears review is promoted within the same sweep, so pages barely spend any time in that intermediate state.

### Top-level orchestration (`run_pipeline.py`)

`run_pipeline()` = `run_cycle()` → `run_consolidation_pass()` → `run_review_sweep()`, in that order, one call — exactly what the systemd timer (`deploy/eoxs-wiki-pipeline.timer`, every 6 hours) invokes. Promotion is no longer excluded from this chain — it now runs automatically as the last step inside `run_review_sweep()` itself, for any page that sweep (or an earlier one) marked `reviewed`.

## 3. How synthesis actually happens (`claude -p` sub-agents)

All three phases share one invocation path: `headless_agent.run_headless_agent(url, prompt, timeout_seconds, max_attempts=5)`, which shells out to:

```
claude -p "<prompt>" \
  --mcp-config <generated config pointing at an HTTP/SSE URL> \
  --strict-mcp-config \
  --disallowedTools "Bash Write Edit Read Task Agent WebFetch WebSearch Glob Grep NotebookEdit" \
  --permission-mode bypassPermissions \
  --output-format stream-json --verbose
```

**No `--model` flag is passed anywhere** — unlike the standalone classifier scripts (`tier_classifier.py`, `citation_llm_resolver.py`), which explicitly call `claude-haiku-4-5-20251001` via the Anthropic SDK, these three synthesis/consolidation/review sub-agents run on whatever `claude -p`'s default model resolves to. This is worth knowing if you're trying to reason about synthesis quality or cost — it isn't pinned in this codebase.

**Incident, 2026-09-04: this used to bill to `ANTHROPIC_API_KEY`, not the Claude Max subscription.** `claude -p` treats `ANTHROPIC_API_KEY` as an auth override that beats the `deploy` user's OAuth login whenever the env var is present in the process's environment — and `headless_agent.py`'s `_invoke_once()` used to spawn the subprocess with no `env=` argument, so it inherited the full parent env (including `ANTHROPIC_API_KEY`, loaded service-wide via systemd's `EnvironmentFile=` for the unrelated `spam_filter.py`/`call_relevance.py` calls). Every sub-agent session was silently billed to that key at `claude-sonnet-5` pricing — confirmed as the root cause of ~$700 in unexpected spend. Fixed by explicitly popping `ANTHROPIC_API_KEY` and `CLASSIFIER_ANTHROPIC_API_KEY` out of the subprocess env before spawning `claude -p`, so this reliably falls through to the OAuth/Max login. See `docs/tech-stack-and-billing.md` §3 (AI / LLM) for the full incident writeup.

**Why `--disallowedTools` and `bypassPermissions` are used together, and why this is safe**: an earlier attempt to sandbox these sub-agents via `--tools ""` or an `--allowedTools` allowlist didn't work reliably — both failed to actually disable built-in tools (Bash/Read/Write/Edit stayed callable), and a long `--disallowedTools` deny-list (~30 names) was found to reliably break MCP tool registration on complex prompts. A short, 11-name deny-list turned out to fix that deterministically — it's the list shown above. `--permission-mode bypassPermissions` means the CLI itself won't prompt for confirmation on any tool call the sub-agent makes — but this is safe *specifically* because the sub-agent is never given `Bash`/`Write`/`Edit`/`Read` at all (they're in the deny-list) and is only ever handed the phase's own narrow, purpose-built MCP server (see §3.1). The actual safety boundary is the small, deliberately-scoped set of MCP tools the sub-agent can call — not the CLI's permission-prompt system, which is bypassed entirely by design.

**Transport (2026-08 change)**: every invocation now connects over HTTP/SSE to `wiki_ingestion/mcp_http_server.py`, one always-on persistent server (`deploy/eoxs-wiki-mcp.service`), instead of `claude -p` spawning a brand-new stdio subprocess per call. This was a real, live incident, not a hypothetical hardening — see §6 for the full story and root cause. Retries: up to 5 attempts (10s apart), with a check for whether the sub-agent ever actually invoked a real `mcp__wiki__*` tool (a much stronger signal than exit code or turn count — see §6). Every call returns `{"ok", "returncode", "stdout", "stderr", "attempts"}` — timeouts and exhausted retries produce `ok: False`, never a raised exception.

### 3.1 What each sub-agent's tools actually are

- **Phase 3** (`agent_mcp_server.build_agent_server(cycle_id, source_kind)`): all read tools from the main `mcp_server` (`search_emails`, `get_email`, `search_calls`, `get_implementation_task`, etc. — the current 17-tool set, see `docs/backend-server.md` §5; `get_ticket`/`search_tickets`/`get_invoice`/`search_invoices` no longer exist here either, removed 2026-08-10 along with the rest of the tool surface) plus write tools scoped only to `wiki_staging`: `search_wiki_inventory`, `get_live_wiki_page`, `create_staging_page`, `update_staging_page`, `add_staging_link`, `add_staging_citation`, `add_staging_flag`. `cycle_id`/`source_kind` are factory arguments closed over when the server is built (one fresh instance per SSE connection, keyed by the URL's `/wiki-agent/{cycle_id}/{source_kind}/sse` path) — never something the model's own tool arguments could fabricate or override.
- **Phase 4** (`consolidate_mcp_server.build_consolidate_server()`): read tools + `get_staging_page`, `merge_staging_pages`. No per-connection scoping needed — every tool takes explicit ids.
- **Phase 5** (`review_mcp_server.build_review_server()`): read tools + `get_staging_page`, `mark_reviewed`, `mark_rejected`. **No promotion tool exists here at all**, by design — review can approve/reject, but never publish.

All three factories build a **fresh `Server` instance per call** (per SSE connection) rather than sharing one mutable instance — `wiki_ingestion/mcp_http_server.py` calls the right factory inside each route's request handler.

## 4. Citations — the mechanism that ties a wiki page back to real data

`wiki_citations(wiki_page_id, source_type, source_id, source_ref_raw)` — same shape in both `public` (live) and `wiki_staging`. `source_type` is one of `email_thread` / `ticket` / `call_transcript` / `implementation_task` / `asset` for anything newly written (older imported rows also carry legacy values like `unresolved`/`wiki_page`, handled by the two repair scripts below, not by new writes). `source_id` is polymorphic — notably, for `implementation_task` it's the **`odoo_task_id`**, never the internal serial `id`, because that table gets fully refreshed every raw-ingestion sweep. Note `add_staging_citation` (in `agent_mcp_server.py`) does not validate `source_type` against this list — it's a plain INSERT, so a sub-agent could in principle write an unrecognized value; §5's access_tier query only recognizes the five listed here.

**Verification is entirely model-driven, not SQL-driven** — the Review phase's prompt tells the sub-agent to call `get_staging_page`, then use matching read tools (`get_ticket`, `get_email`, etc.) to confirm each citation is accurate. There is no deterministic/SQL-level "does this citation's target actually exist and say what the page claims" check anywhere in the driver code (`review.py`/`run_review.py`) — it's fully delegated to the model's own tool-call reasoning during that sub-agent session.

**Legacy citation repair** (one-off/periodic, not part of the recurring 6-hourly pipeline):
- `citation_resolver.py` — non-LLM, pattern-matches `unresolved` legacy citations against `email_threads` by keyword overlap within a date window.
- `citation_llm_resolver.py` — LLM-assisted second pass, **two-stage adversarial verification**: a first call proposes a match (only `HIGH` confidence accepted), a second, independent call tries to *refute* it, defaulting to `REFUTED` on any doubt or API failure. This two-stage design exists because a single-stage version was smoke-tested and got 2 of 5 wrong (40% false positives).

## 5. How a wiki page's `access_tier` gets computed

Computed at promotion time (`promote.py`), as the **most restrictive tier among every citation on the page**:

```sql
SELECT t.access_tier::text FROM wiki_citations wc JOIN email_threads t
  ON wc.source_type='email_thread' AND t.id=wc.source_id WHERE wc.wiki_page_id=%s
UNION SELECT ... FROM call_transcripts ...
UNION SELECT ... FROM tickets ...
UNION SELECT ... FROM implementation_tasks t ON t.odoo_task_id = wc.source_id ...
UNION SELECT ... FROM assets t ON t.id = wc.source_id ...
UNION SELECT ... FROM repo_docs t ON t.id = wc.source_id ...
```
then: `tier1` if any citation is tier1, else `tier2_confidential` if any is that, else `tier2`. One salary figure cited anywhere on a page is enough to restrict the entire page. A known collision risk: implementation-task citations key on `odoo_task_id`, which isn't unique across different clients — the join fails closed (whichever candidate, across any client sharing that id number, is most restrictive wins).

**2026-08-25 fix**: the `assets` branch above was missing entirely until this date — added after a health audit found 32 live `wiki_citations` rows already using `source_type='asset'` (the assets ingestion path has been writing them since 2026-08-12) with no corresponding join, meaning a page citing only assets — including either of the two `tier2_confidential` assets (`eoxs-client-implementation-go-live-sop`, `eoxs-salary-details`) — would silently compute an empty `cited_tiers` set and default to the least-restrictive `tier2`. No page was actually mis-tiered by this in practice (the 3 asset-sourced pages promoted before the fix all happened to cite only `tier2` assets), but the gap was live and unguarded. Fixed in `wiki_ingestion/promote.py`.

**2026-08-28 fix, same bug class, caught before any page shipped**: adding the `repo_docs` partition to detection (above) meant the drafting sub-agent could now cite `repo_docs` rows, but neither `agent_mcp_server.py`'s `add_staging_citation` tool schema nor this join recognized `source_type='repo_doc'` — every `repo_docs` row is hardcoded `tier1`, so a page citing only repo docs would have silently promoted to `tier2`, exposing internal engineering/infra detail (credentials layout, schema internals, redaction logic, per-identity secrets) to `hr`/`general`/`intern`. Fixed in the same commit that added the partition: the join above gained the `repo_docs` branch, the tool schema/description now names `'repo_doc'` explicitly, and `run_agent.py`'s prompt gained a `repo_docs`-specific notes block instructing the agent to always use `source_type='repo_doc'` for these rows, never `'asset'` or anything else.

A **separate** classifier, `tier_classifier.py`, exists purely for the ~1,046 legacy pages imported before `promote.py` existed (whose citations are mostly unresolved, so the citation-max approach alone can't classify most of them). It runs an LLM content-classification pass first (fail-closed to `tier1` on error), then two SQL-only backstop passes that only ever *tighten* a tier, never loosen it: one upgrading from resolved raw citations, one propagating tier along wiki-page-cites-wiki-page links to a fixed point.

## 6. 2026-08 incident — real, root-caused, and fixed

**Symptom**: for ~2 days (Aug 4–6), the pipeline ran on schedule every 6 hours, reported `status: done`, and correctly detected real new raw data every cycle — but produced **zero** new or updated `wiki_staging` pages, and the same 4 pages (ids 275–278, stuck in `draft` since cycle 13) got silently "re-reviewed" as `ok: true` across 8 consecutive cycles without ever actually being marked reviewed or rejected.

**Root cause, found by direct reproduction, not inference**: `agent_mcp_server.py`/`review_mcp_server.py`/`consolidate_mcp_server.py`'s `list_tools()` handlers called `await read_tools.list_tools()` — but `mcp_server.server` (the module they import as `read_tools`) has no such function. Its tool-definition list is only ever built via a private `_tool_defs()` helper, called *inside* `build_server()`'s own closure-registered handler, never exposed as a standalone importable coroutine. Every `tools/list` request to any of the three wiki-ingestion MCP servers therefore threw `AttributeError: module 'mcp_server.server' has no attribute 'list_tools'` — confirmed directly by hand-driving the MCP protocol over HTTP and reading the raw JSON-RPC error back. The sub-agent, correctly, ends up with zero usable tools and explains so in its final message rather than fabricating content — but since it still takes several real turns to reach that conclusion, the original `num_turns<=1` failure heuristic never caught it, and the batch was recorded `ok: true`.

**Fix, verified live**: the three `list_tools()` handlers now call `read_tools._tool_defs()` (synchronous, no `await`) instead. Also hardened `headless_agent.py` so this class of failure can never again masquerade as success: it now parses the full `stream-json` transcript and requires at least one real `mcp__wiki__*` tool invocation before accepting a run as `ok: true` — a batch that exits cleanly but never touches a wiki tool is now correctly reported as failed (`mcp_never_connected: True`), regardless of exit code or turn count. Verified by re-running the exact failing scenario before and after the fix, and by re-running the review sweep against the real stuck pages 275–278 afterward — all 4 finally left `draft` (7 reviewed, 2 rejected across that real batch, including the two duplicate/malformed drafts created during testing).

**Root-level transport fix, done at the same time**: independently of the bug above, the whole class of "`claude -p` connects to a freshly-spawned stdio MCP subprocess" was replaced with the same persistent-HTTP-server pattern already proven reliable for the external claude.ai connector (`mcp_server/http_server.py`). See §3 and `deploy/eoxs-wiki-mcp.service`. This wasn't the root cause of this specific incident, but stdio-per-invocation is inherently more fragile (a fresh connection handshake every single call) than one always-on server being reached repeatedly, so it was worth fixing regardless.

## 6.1 2026-08-13 incident — a crash with no failure handler, fixed with two layers

**Symptom**: cycle 46 (started 18:01:46) died 1 second after starting, and its Linear parent issue (EDB-537) sat permanently stuck — title still literally reading "running", state "Todo", zero child issues — because nothing existed to notice the process was gone and close it out. The next scheduled cycle (47) started normally 6 hours later regardless (systemd's oneshot semantics don't care about this codebase's own DB bookkeeping), so the *pipeline* kept working — only the one crashed cycle's record and Linear issue were left stale.

**Root cause, found via `journalctl -u eoxs-wiki-pipeline.service`**: `detect.py`'s Gmail/Zoho account list had just been made dynamic (schema/026-029, so a newly self-serve-connected account is picked up automatically), but `email_threads.source_account` was still the old fixed 4-value Postgres enum at that exact moment — the migration converting it to `TEXT` (schema/030) didn't land until later that same evening. `isha_gmail` wasn't a valid enum member yet, so `candidates_email()`'s own `WHERE source_account = 'isha_gmail'` query raised `psycopg2.errors.InvalidTextRepresentation` — an unhandled exception with **no try/except anywhere between `run_pipeline.py`'s call and the module entrypoint**, so the whole process died, and `wiki_ingest_cycles.status` stayed `'running'` forever with no code path that would ever revisit it.

**Fix, two layers, both in `run_cycle.py`/`linear_report.py`**:
1. **Catch at the source**: `run_cycle()` and `resume_cycle()` now wrap their whole body in try/except. Any unhandled exception marks the cycle `'failed'` with the full traceback recorded in `wiki_ingest_cycles.summary` (queryable — `SELECT summary FROM wiki_ingest_cycles WHERE status='failed'` shows exactly why, no journalctl archaeology needed) and updates the Linear parent issue via the new `fail_cycle_parent()` to a clear "CRASHED" title with the traceback in the body, then **re-raises** — the process still exits non-zero and journalctl still shows it, this only adds the missing self-report. Same pattern applied to Phase 4 (`run_consolidation.py`) and Phase 5 (`run_review.py`), which had the identical gap even though neither has crashed yet.
2. **Self-healing for what layer 1 can't catch** (a hard kill / OOM — no Python code runs at all in that case): `run_cycle()` now calls `reap_orphaned_cycles()` before starting a new cycle. Since `eoxs-wiki-pipeline.service` is `Type=oneshot`, systemd guarantees only one invocation runs at a time — so any *other* `wiki_ingest_cycles` row still `'running'` when a new cycle starts is provably dead (if it were alive, systemd wouldn't have started a new one), and gets marked `'failed'` + its Linear issue updated automatically. This is what will catch the next occurrence of this exact failure mode (cycle 46's specific bug can't recur — the enum fix already landed — but a crash from some *other* future cause absolutely still could).

Cycle 46 itself was fixed manually the same way (`_finish_cycle` + `fail_cycle_parent`, using the real captured traceback rather than the generic orphan message) — done directly rather than via `reap_orphaned_cycles()`, since cycle 47 was still genuinely running at the time and that function marks *every* `'running'` row, not just a specific one.

Phase 4/5 have no DB-side "running" bookkeeping row the way `wiki_ingest_cycles` does for Phase 3 (they only ever reported to Linear, nothing persisted to a table first) — so they only got layer 1 (catch-and-report), not an equivalent orphan-reaping self-heal. Worth building if either phase ever actually crashes; not done speculatively.

## 7. Scheduling

`deploy/eoxs-wiki-pipeline.timer`: `OnCalendar=00/6:00:00`, `RandomizedDelaySec=120`, `Persistent=true` — every 6 hours. `deploy/eoxs-wiki-pipeline.service` sets `TimeoutStartSec=infinity` deliberately (a single batch can take 3–5+ minutes, a full run can span hours — systemd's default ~90s oneshot timeout would otherwise kill it mid-run). It now depends on `deploy/eoxs-wiki-mcp.service` (`After=`/`Wants=`) — the persistent HTTP MCP server every sub-agent invocation connects to (see §3, §6) must be up first. `eoxs-wiki-mcp.service` is `Restart=always`, matching `eoxs-mcp.service`'s pattern for the external connector.

## 8. Current real pipeline state (as of 2026-08-25)

`wiki_staging.wiki_pages` status counts: **1,153 promoted**, **51 rejected**, **0 draft**, **0 reviewed**. Zero rows sitting in `draft` or `reviewed` is expected now, not a sign of a stall — see §2's Promotion section: a page clears `draft` → `reviewed` → `promoted` within the same review sweep, so those two intermediate states are normally empty between sweeps. Live `wiki_pages`: **1,866** total (50 tier1 / 1,096 tier2_confidential / 720 tier2). `wiki_ingest_cycles` has reached **98** total cycles.

Treat any numeric snapshot in this section (including this one) as a point-in-time reading, not a maintained fact — it goes stale within days on a system this active. Query `wiki_staging.wiki_pages`/`public.wiki_pages`/`wiki_ingest_cycles` directly, or check the live "pending drafts" Linear board (`docs/linear-integration.md` §9), for the current numbers.

**History**: an earlier snapshot of this document recorded only 2 pages ever promoted against 210 sitting in `reviewed` — a large backlog that had been silently stuck (traced to an unpopped `git stash` from 2026-08-04 that had, among other things, removed `promote.py` from the working tree entirely). Once recovered and re-run, the full `reviewed` backlog cleared in one pass with zero failures. Promotion was later made fully automatic (see §2), which is why `reviewed` no longer accumulates a backlog at all.

## 9. Linear reporting hook

2026-08 redesign — see `docs/linear-integration.md` for the full mechanism. Every phase now creates a **parent** Linear issue the moment it starts (not after it finishes) and one **child** Linear task per actual sub-agent invocation — one per Phase 3 chunk, one per Phase 4 duplicate group, one per Phase 5 review chunk — created right before that specific `claude -p` call and updated immediately after with its real, full outcome: exactly which rows/pages it was given, and either the complete body text of every page it touched or a specific, non-generic failure reason (never a bare "failed"). `promote_reviewed_pages()` still reports a single summary issue per promotion batch (one issue per automatic promotion run, not per-page — see §2 for why this now runs after every review sweep rather than manually/infrequently), and the persistent "pending drafts" board (§ same as before) still refreshes on every review sweep regardless of what that sweep did.

## 10. Code vs. AI split

By line count: **~960 lines (≈31%)** directly orchestrate AI calls (`headless_agent.py`, `run_agent.py`, `run_consolidation.py`, `run_review.py`, plus the two standalone SDK-based classifiers `citation_llm_resolver.py`/`tier_classifier.py`). **~2,119 lines (≈69%)** are deterministic plumbing — detection/hashing (`detect.py`), cycle bookkeeping (`run_cycle.py`), promotion SQL (`promote.py`), the three MCP tool servers, Linear reporting, and the pattern-based citation repair.

That said, this split understates how much of the actual *thinking* is AI-driven: nearly all real judgment (what to write, how to deduplicate, whether a citation checks out, whether a page is good enough to approve) happens **inside** the opaque `claude -p` sub-agent processes that the "deterministic" 69% merely spawns, feeds a prompt to, and records the exit status of. Line count measures orchestration code, not where the actual decisions get made.
