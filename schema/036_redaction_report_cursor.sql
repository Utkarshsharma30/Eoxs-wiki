-- Tracks how far ingestion/redaction_linear_report.py has reported into
-- mcp_redaction_log, so each 2-hourly sweep only creates Linear issues for
-- genuinely new redaction events since the last run, not the whole table
-- again. One row, updated in place -- same "small persistent cursor" shape
-- as wiki_ingest_board_state (schema/021), but tracking a row id instead of
-- a Linear issue id.
CREATE TABLE redaction_report_cursor (
    id                  TEXT PRIMARY KEY DEFAULT 'singleton',
    last_reported_id    INTEGER NOT NULL DEFAULT 0,   -- highest mcp_redaction_log.id already reported to Linear
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO redaction_report_cursor (id, last_reported_id) VALUES ('singleton', 0);
