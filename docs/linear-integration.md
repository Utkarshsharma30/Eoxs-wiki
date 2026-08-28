# Linear Integration

*Deep technical reference for how Cruz (`eoxs-wiki-db`) reports its automated activity to Linear (team: EDB) — for developers being onboarded to this piece, or setting it up fresh on a new VPS.*

## 1. What this is, and what it isn't

This is a **one-way reporting sink**. Cruz's automated pipelines (raw ingestion sweeps, wiki-ingestion cycles) push status updates into a Linear workspace so anyone can check what the automation has been doing without needing database access. It is **not** a control surface today — nothing in Cruz ever reads a comment, a status change, or any other signal back out of Linear. If you want to promote a draft wiki page or trigger a manual sweep, that still happens by running a command against the server/database directly, not by doing anything in Linear.

## 2. How to set this up from scratch on a new VPS

1. **Create a team in the Linear UI manually** — this system was originally set up this way (a team with key `EDB`), and there is no team-creation code anywhere in this repo (confirmed: no `teamCreate` mutation exists in the codebase). Do this by hand in Linear's own UI.
2. **Generate a personal/workspace API key** in Linear's settings and put it in `.env` as `LINEAR_EDB_API_KEY`.
3. **Set `LINEAR_EDB_TEAM_KEY`** in `.env` to the team's key (defaults to `"EDB"` in code if unset, but set it explicitly to be safe).
4. That's it — no other setup exists. The code resolves the team's internal Linear ID and workflow-state IDs (Todo/Done) at runtime by querying the API, so nothing else needs to be hardcoded or pre-provisioned.

### Important env-var naming gotcha

`.env` currently holds **four** Linear-related variables, but only two of them are actually read by the running code:

| Variable | Actually used? |
|---|---|
| `LINEAR_EDB_API_KEY` | **Yes** — this is the real key `linear_report.py` sends as the `Authorization` header. |
| `LINEAR_EDB_TEAM_KEY` | **Yes** — this is the real team key used to resolve the team ID. |
| `LINEAR_API_KEY` | No — unused by any code path today. |
| `LINEAR_TEAM_KEY` | No — unused by any code path today. |

The `_EDB_`-less pair is a leftover from an earlier plan (documented in `HANDOFF.md` §7 item 6, written before `linear_report.py` was actually built) — the project's own env-completeness diagnostic (`ingestion/_check_env.py`) still references the older, unused names, so **don't trust `_check_env.py`'s output for Linear specifically** — verify against `ingestion/linear_report.py` directly if you're checking whether Linear reporting is configured correctly on a new deployment.

## 3. The API client

There is exactly **one** GraphQL client in the whole codebase: `ingestion/linear_report.py`. `wiki_ingestion/linear_report.py` imports its low-level functions (`_create_issue`, `_update_issue`) rather than duplicating them — both raw-ingestion and wiki-ingestion reporting share the same underlying HTTP client and the same Linear team.

- **Endpoint**: `https://api.linear.app/graphql`, called directly via `httpx.post()` — no Linear SDK dependency.
- **Auth header**: `Authorization: <the raw API key>` — no `Bearer` prefix (Linear expects the key unprefixed).
- **Team resolution**: `_get_team_id()` queries `teams(filter: {key: {eq: $key}})`, caches the result in-process for the life of the run (`_team_id_cache`).
- **Workflow state resolution**: `_get_state_id()` queries the team's `states { id name }`, caches by name (`_state_id_cache`). This matters because creating an issue with no explicit `stateId` would default to the team's initial state (typically Backlog), which would make a completed, successful automation run look like unstarted work in the Linear UI — so every report explicitly resolves and sets `"Todo"` (if there were errors) or `"Done"` (if clean).

**Read-only queries** used (only 2, both pure metadata lookups needed to build mutations — never used to read issue content back):
```graphql
query($key: String!) { teams(filter: {key: {eq: $key}}) { nodes { id key } } }
query($teamKey: String!) { teams(filter: {key: {eq: $teamKey}}) { nodes { states { nodes { id name } } } } }
```

**The two mutations** used everywhere:
```graphql
mutation($teamId: String!, $title: String!, $description: String!, $stateId: String) {
  issueCreate(input: {teamId: $teamId, title: $title, description: $description, stateId: $stateId}) {
    success
    issue { id identifier url }
  }
}

mutation($id: String!, $title: String!, $description: String!, $stateId: String) {
  issueUpdate(id: $id, input: {title: $title, description: $description, stateId: $stateId}) {
    success
    issue { id identifier url }
  }
}
```

## 4. What gets reported for RAW ingestion — one parent per sweep, one child task per source

**2026-08 redesign**, driven by a real incident where an aggregate-only report couldn't show which specific automated step actually failed (see `docs/wiki-ingestion.md` §6). `ingestion/server.py`'s `run_full_sweep()` now:
1. Calls `start_sweep_parent()` **before** the first source runs — creates a `"... — running"` parent issue immediately, so a stuck sweep is visible while in flight, not just after.
2. For each of the 7 sources, calls `start_source_task(parent_id, name)` right before that source's fetch function runs, then `finish_source_task(issue_id, name, detail)` right after — a real Linear sub-issue (`parentId` set to the sweep's parent) per source, showing its target table(s), and after completion, its exact row counts and up to 10 real item names (from that source's own `written_items`), per-account/per-client broken out in a table for Gmail and the implementation boards.
3. Calls `finish_sweep_parent(parent_id, summary, run_at)` at the end — rolls the parent issue up to its final title/state, pointing at the child sub-issues for detail.

Called from exactly two places, same as before: `ingestion/server.py`'s `--sweep` CLI path (the real 2-hourly cron entrypoint) and the `/trigger/manual` HTTP route — individual webhook triggers (`/webhook/gmail`, `/webhook/fireflies`, `/webhook/fathom`) call their fetcher directly, never `run_full_sweep()`, so a single incoming email still never touches Linear. The old single-issue `report_full_sweep()` function still exists in `ingestion/linear_report.py` for any external caller, but nothing in this codebase calls it anymore.

## 5. What gets reported for WIKI ingestion — one parent per phase run, one child task per sub-agent call

**2026-08 redesign**, same rationale as raw ingestion above, and even more directly motivated by it — the whole incident in `docs/wiki-ingestion.md` §6 was invisible under the old aggregate-only reports. `wiki_ingestion/linear_report.py` now creates a parent issue at the **start** of each phase run and one child Linear task per actual `claude -p` sub-agent invocation, created right before that specific call and updated immediately after with its complete real outcome:

- **Phase 3** (`start_cycle_parent`/`finish_cycle_parent`, `start_batch_task`/`finish_batch_task`) — one parent per cycle, one child per chunk. A successful child's body has the **full body text** of every page it created or updated, labeled CREATED/UPDATED; a 0-page child says so explicitly ("nothing worth drafting"); a failed child states the specific reason (e.g. "MCP server never finished connecting" vs. a captured stderr snippet) — never a bare "failed". `wiki_ingest_cycles.linear_parent_issue_id` and `wiki_ingest_batches.linear_issue_id` (previously dead columns, see the note in `docs/postgres-database.md`) are now genuinely written and read.
- **Phase 4** (`start_consolidation_parent`/`finish_consolidation_parent`, `start_consolidation_task`/`finish_consolidation_task`) — one parent per pass, one child per duplicate group, showing the merged page's full resulting body on success.
- **Phase 5** (`start_review_parent`/`finish_review_parent`, `start_review_task`/`finish_review_task`) — one parent per sweep, one child per chunk, showing every page's full content plus its verdict (APPROVED/REJECTED) and the reviewer's actual notes/reason. Also still unconditionally refreshes the persistent pending-drafts board below, regardless of what this specific sweep did (a promotion elsewhere can also change the pending count).
- **Promotion** (`report_promotion()`) — still a single summary issue per promotion batch, not per-page. **This is no longer because promotion is manual/infrequent** — as of the same 2026-08 change, `run_review_sweep()` calls `promote_reviewed_pages()` automatically at the end of every review sweep, so a promotion issue now fires just as often as a review sweep does (see `docs/wiki-ingestion.md` §2's Promotion section). One summary issue per batch remains the right level of detail; only the *trigger* changed from manual to automatic.

Every `start_*`/`finish_*` call is wrapped in the same `_safe()` never-raises pattern as before (see §7) — a Linear outage degrades to a logged warning, never a failed pipeline run.

### The persistent "pending drafts" board — update-in-place, not a new issue every time

Backed by `wiki_ingest_board_state(board_key PRIMARY KEY, linear_issue_id, linear_issue_identifier, updated_at)`. `report_pending_drafts_board()`:
1. Queries `wiki_staging.wiki_pages` live for everything `reviewed` (awaiting a promote decision) and everything `rejected`.
2. Renders one full itemized markdown document — reviewed pages grouped by source, plus a rejected-pages table with reasons.
3. Looks up `board_key = 'pending_drafts'` in `wiki_ingest_board_state`. If a row exists, calls `issueUpdate` on that same Linear issue — same issue, every time, just refreshed content. If no row exists yet (first-ever call), creates a new issue and records its id in `wiki_ingest_board_state` for every future call to reuse.

`board_key` is designed to support more than one persistent board in the future (only `pending_drafts` exists today).

### `linear_parent_issue_id` / `linear_issue_id` — genuinely live, not dead columns

An earlier version of this document called `wiki_ingest_cycles.linear_parent_issue_id` and `wiki_ingest_batches.linear_issue_id` "dead columns... never read or written anywhere in the Python codebase, always NULL today." **That was already false when written** — it directly contradicted the paragraph above it in this same section (§5's Phase 3 bullet), and a 2026-08-25 health audit confirmed it live: `wiki_ingest_cycles.linear_parent_issue_id` is populated on 66 of 92 rows, `wiki_ingest_batches.linear_issue_id` on 368 of 517, and `wiki_ingestion/run_cycle.py` actively writes both (on cycle/batch start) and reads `linear_parent_issue_id` back for orphan-cycle recovery (`reap_orphaned_cycles()` → `fail_cycle_parent()`). These columns hold real data and are load-bearing for crash recovery — do not treat them as unused.

## 6. Read-back — confirmed one-way

There is no `/webhook/linear` route, and no code anywhere queries Linear for comments, state changes, or any other kind of instruction. The only two GraphQL *queries* in the entire integration (§3) are pure metadata lookups needed to construct mutations. If you want Cruz to react to something happening in Linear (e.g., "comment `/promote` on an issue to trigger a real promotion"), that would be new code — nothing like it exists today.

## 7. Error handling — a Linear failure can never break a pipeline run

Every reporting call is wrapped in a try/except that logs a warning and returns — never raises past the report function itself. `report_full_sweep()`'s own docstring states the discipline directly: *"Never raises -- a Linear reporting failure should never fail or mask an otherwise-successful ingestion run."* If `LINEAR_EDB_API_KEY` isn't set at all, the function short-circuits with a warning before making any HTTP call. Rate limits, network errors, invalid keys, and GraphQL-level errors all degrade to a logged warning — the underlying sweep or wiki-ingestion phase always completes normally regardless of Linear's availability.

## 8. An official Linear MCP server is also available

Separate from this project's own hand-rolled GraphQL client, this environment has access to a full official Linear MCP tool surface (`list_issues`, `get_issue`, `save_issue`, `list_comments`, `save_comment`, `list_projects`, `list_cycles`, `list_documents`, and many more — including attachment and diff-review tools). This gives genuine two-way capability (reading and reacting to comments, for example) that the current custom integration deliberately doesn't implement. A future developer wanting to add read-back capability (see §6) could build on this official surface directly instead of extending the hand-rolled GraphQL client — worth knowing about before writing a second, redundant Linear HTTP client from scratch.

## 9. Current real state (as of 2026-08-28)

`wiki_ingest_board_state` has one row: `board_key='pending_drafts'`, pointing at Linear issue `EDB-37`. `ingest_log` shows the 2-hourly raw-ingestion sweep firing reliably. `wiki_ingest_cycles` shows the 6-hourly wiki pipeline running on schedule (roughly 00:00/06:00/12:00/18:00 UTC — cycle 107 as of this writing, all recent cycles `status='done'`). `linear_parent_issue_id` is populated on 75 of 101 cycles; `wiki_ingest_batches.linear_issue_id` on 405 of 555 — both ratios climbing as expected (recent cycles have parent/batch issues; the unpopulated ones are early rows from before this reporting existed).

`EDB-37`'s live title as of 2026-08-28 (fetched directly via the Linear GraphQL API, `LINEAR_EDB_API_KEY`/`LINEAR_EDB_TEAM_KEY`) reads **"[BOARD] Wiki drafts pending approval (0 awaiting, 55 rejected)"**, `updatedAt` 2026-08-28T06:11:35Z — matching the last wiki-pipeline cycle's finish time exactly, confirming the board genuinely refreshes every cycle rather than just holding a stale title. 0 awaiting is expected, not a sign of a stalled board: promotion runs automatically after every review sweep (see `docs/wiki-ingestion.md` §2), so pages rarely linger in `reviewed` status long enough to accumulate a backlog the way they could when promotion was a manual, human-triggered step. Treat any number in this section as a point-in-time reading — query `wiki_staging.wiki_pages` or fetch `EDB-37` live for the current count, don't rely on what's written here.

**A known, separately-tracked staleness**: `wiki_ingest_board_state.updated_at` for this row is stuck at `2026-08-04`, even though the actual Linear issue (`EDB-37`) is genuinely current — confirmed by fetching it live above. `report_pending_drafts_board()`'s `_set_board_issue()` (which sets `updated_at = now()`) is only called on the first-ever creation of the board issue; every subsequent refresh only calls Linear's `issueUpdate` and never touches the DB row's `updated_at` again. Harmless in practice (the Linear issue itself is what matters and it does refresh correctly), but don't use this column to judge whether the board is being kept current — check the Linear issue directly instead.

## 10. Who to reach out to for credentials

**Ayan and Nidhi** hold the Linear API key and team access for the EDB workspace.

## 11. Key files, in reading order for a new developer

1. `ingestion/linear_report.py` — the GraphQL client + raw-sweep report (360 lines)
2. `wiki_ingestion/linear_report.py` — the four phase reports + persistent board (564 lines)
3. `schema/021_wiki_board_state.sql` — the `wiki_ingest_board_state` table definition
4. `ingestion/server.py` (the `_run_bg`/`main`/`/trigger/manual` sections) — where/how raw-sweep reporting actually gets triggered
5. `deploy/eoxs-sweep.timer`, `deploy/eoxs-wiki-pipeline.timer` — the two cron cadences that ultimately drive all of this
