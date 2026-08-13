-- Audit trail for every create/update made through the new asset-write MCP
-- tools (mcp_server/asset_writes.py) -- same purpose and shape as
-- employee_change_log (schema/025), so every edit to a curated internal
-- document (SOPs, company overview, salary register, etc.) is attributable
-- to which identity made it and what actually changed, not just the
-- current row state. Unlike employee_change_log's field-level diffs, this
-- stores the full old/new title and body text per change -- these documents
-- are a "handful of long documents" (schema/031's own words), not tens of
-- thousands of rows, so a real version history is affordable and, for a
-- document like the salary register, genuinely valuable on its own merits.
CREATE TABLE asset_change_log (
    id              SERIAL PRIMARY KEY,
    asset_id        INTEGER NOT NULL REFERENCES assets(id),
    changed_by      TEXT NOT NULL,   -- MCP identity name: 'full' (Raj) or 'hr' (Isha) -- bound at
                                      -- server-construction time, never taken from a tool call's own arguments
    change_type     TEXT NOT NULL,   -- 'created' | 'updated'
    changes         JSONB,           -- {field: {old, new}} -- full old/new text for 'title'/'body'
    occurred_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_asset_change_log_asset_id ON asset_change_log(asset_id);
