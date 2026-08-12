-- Employee directory (Cruz): a separate operational table, deliberately NOT
-- part of the wiki-ingestion pipeline -- never touched by detect.py,
-- promote.py, or wiki_pages/wiki_staging. This is directly-written reference
-- data, the same category as clients/contacts (schema/002_reference.sql), not
-- AI-synthesized content. Source of truth going forward is this table itself,
-- kept current via the create_employee/update_employee/deactivate_employee/
-- reactivate_employee MCP tools -- gated to Raj's `full` identity and Isha's
-- `hr` identity only, see mcp_server/employees.py and the enable_employee_tools
-- flag in mcp_server/server.py's build_server(). No other identity (general,
-- intern) can read or write this table through Cruz at all.
--
-- status is a soft-delete flag, never a real DELETE: 'active' is the default
-- filter for list/search (current headcount), 'inactive' (left the company)
-- stays fully queryable via an explicit status='inactive'/'all' request for
-- historical lookups. Matches the mark_tasks_inactive() soft-delete pattern
-- implementation_tasks already uses (see docs/raw-ingestion.md #3).
--
-- Deliberately excluded: any password/credential field. The source
-- spreadsheet this table was seeded from had a plaintext-password column for
-- personal Gmail accounts -- not imported, on purpose, regardless of how this
-- table gets used later.
CREATE TYPE employee_status AS ENUM ('active', 'inactive');

CREATE TABLE employees (
    id                  SERIAL PRIMARY KEY,
    full_name           TEXT NOT NULL,
    department          TEXT,
    role_title          TEXT,
    employment_type     TEXT,   -- e.g. 'full_time' | 'intern' | 'contractor' | 'virtual_assistant' --
                                 -- plain text, not an enum: this vocabulary already varies across the
                                 -- source data and will keep growing, unlike access_tier's fixed 3 values
    official_email      TEXT,
    manager             TEXT,   -- freetext name (e.g. 'Raj Sir', 'Dhrup') -- matches how the source
                                 -- spreadsheets track it; not a self-referencing FK to another employee row
    date_of_joining     DATE,
    date_of_leaving     DATE,
    status              employee_status NOT NULL DEFAULT 'active',
    notes                TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Partial unique: most rows have an official email, but not all (a few source
-- rows had none) -- NULLs are never compared equal in Postgres uniqueness, so
-- an ordinary UNIQUE column constraint would already behave this way, but the
-- WHERE clause makes that explicit rather than incidental.
CREATE UNIQUE INDEX idx_employees_official_email ON employees(official_email) WHERE official_email IS NOT NULL;
CREATE INDEX idx_employees_status ON employees(status);
CREATE INDEX idx_employees_department ON employees(department);
CREATE INDEX idx_employees_name_trgm ON employees USING gin (full_name gin_trgm_ops);

-- Audit trail for every create/update/deactivate/reactivate made through the
-- MCP write tools -- same purpose as mcp_redaction_log (schema/023), so every
-- change to an employee record is attributable to which identity made it and
-- what actually changed, not just the current row state.
CREATE TABLE employee_change_log (
    id              SERIAL PRIMARY KEY,
    employee_id     INTEGER NOT NULL REFERENCES employees(id),
    changed_by      TEXT NOT NULL,   -- MCP identity name: 'full' (Raj) or 'hr' (Isha) -- bound at
                                      -- server-construction time, never taken from tool-call arguments
    change_type     TEXT NOT NULL,   -- 'created' | 'updated' | 'deactivated' | 'reactivated'
    changes         JSONB,           -- {field: {old, new}} for updates/deactivate/reactivate;
                                      -- full field snapshot for created
    occurred_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_employee_change_log_employee_id ON employee_change_log(employee_id);
