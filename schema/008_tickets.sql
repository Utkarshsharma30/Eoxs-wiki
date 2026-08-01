-- Odoo support tickets. Source: raw/tickets/*.md
-- Per CLAUDE.md/exploration notes: frontmatter is authoritative; the body's
-- recap table can drift (e.g. status) and is not re-parsed into columns.

CREATE TABLE tickets (
    id                  SERIAL PRIMARY KEY,
    odoo_id             INTEGER,
    ticket_number       TEXT NOT NULL UNIQUE,   -- e.g. 'T03334'
    client_raw          TEXT,                    -- frontmatter 'client', freetext (e.g. 'Velox', sometimes 'Unknown')
    client_id           INTEGER REFERENCES clients(id) ON DELETE SET NULL, -- best-effort resolved match
    subject             TEXT,
    status              TEXT,
    priority            TEXT,
    assigned_to         TEXT,
    ticket_created       DATE,
    ticket_closed        DATE,
    tags                TEXT[] NOT NULL DEFAULT '{}',
    description         TEXT,
    generated_at        TIMESTAMPTZ,
    source_file_path    TEXT NOT NULL UNIQUE,
    source_file_mtime   TIMESTAMPTZ NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_tickets_client_id ON tickets(client_id);
CREATE INDEX idx_tickets_status ON tickets(status);
CREATE INDEX idx_tickets_subject_trgm ON tickets USING gin (subject gin_trgm_ops);

-- One row per Activity Thread entry (Log Note or ownership/stage change).
CREATE TABLE ticket_events (
    id              SERIAL PRIMARY KEY,
    ticket_id       INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    odoo_msg_id     BIGINT,               -- from '<!-- odoo_msg_id:NNNNN -->'
    event_type      TEXT NOT NULL,        -- 'log_note' | 'ownership_change' | 'other'
    author          TEXT,
    event_time      TIMESTAMPTZ,
    body            TEXT NOT NULL,
    event_order     INTEGER NOT NULL      -- position within the thread, for stable ordering
);

CREATE INDEX idx_ticket_events_ticket_id ON ticket_events(ticket_id);

CREATE TABLE ticket_attachments (
    id              SERIAL PRIMARY KEY,
    ticket_id       INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    filename        TEXT NOT NULL,
    relative_path   TEXT NOT NULL   -- raw/tickets/attachments/<TICKET_ID>/<filename>
);

CREATE INDEX idx_ticket_attachments_ticket_id ON ticket_attachments(ticket_id);
