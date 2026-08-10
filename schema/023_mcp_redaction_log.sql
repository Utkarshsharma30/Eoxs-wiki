-- Audit trail for mcp_server/redaction.py's query-time redaction safety
-- net -- records every time that layer actually strips content from a
-- tool response before it reached a non-full-clearance caller. This is
-- a SECOND, independent check on top of the existing access_tier
-- filtering (never a replacement for it): every response it sees has
-- already passed the normal SQL tier filter, so a row appearing here
-- means the ORIGINAL classification (ingestion/inline_tier_classifier.py,
-- wiki_ingestion/promote.py, wiki_ingestion/tier_classifier.py) was wrong
-- about that specific content -- the point of this table is to make that
-- visible so the source can be fixed, not to rely on this catching the
-- same mistake forever.
CREATE TABLE mcp_redaction_log (
    id                  SERIAL PRIMARY KEY,
    occurred_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    clearance_name      TEXT NOT NULL,       -- 'hr' | 'general' (never 'full' -- that identity skips this check)
    tool_name           TEXT NOT NULL,
    redacted_snippets   TEXT[] NOT NULL      -- the exact spans that were removed, for follow-up investigation
);

CREATE INDEX idx_mcp_redaction_log_occurred_at ON mcp_redaction_log (occurred_at);
CREATE INDEX idx_mcp_redaction_log_tool_name ON mcp_redaction_log (tool_name);
