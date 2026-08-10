-- Tracks the Linear issue id backing each persistent "board" issue --
-- e.g. the pending-drafts board (everything currently awaiting promotion
-- approval, everything currently rejected, fully itemized). These boards
-- are updated IN PLACE every wiki-ingestion cycle (see
-- wiki_ingestion/linear_report.py's report_pending_drafts_board()) rather
-- than spawning a new Linear issue every run -- one persistent, always-
-- current place to look, not a growing pile of stale snapshots.
CREATE TABLE wiki_ingest_board_state (
    board_key                TEXT PRIMARY KEY,   -- e.g. 'pending_drafts'
    linear_issue_id          TEXT NOT NULL,       -- Linear's internal id, used for issueUpdate
    linear_issue_identifier  TEXT NOT NULL,       -- human-readable, e.g. 'EDB-40'
    updated_at                TIMESTAMPTZ NOT NULL DEFAULT now()
);
