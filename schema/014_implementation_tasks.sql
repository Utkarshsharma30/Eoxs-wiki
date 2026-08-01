-- Odoo implementation Kanban tasks, per-client (greer, ess, dps, ppc, 3gm,
-- sabre -- the 6 of 8 clients with a live Odoo instance, per
-- clients.odoo_base_url/odoo_db). Source: tools/odoo_implementation_to_obsidian.py
-- in the old pipeline, writing to raw/clients/<slug>/implementation/*.md.
--
-- Unlike every other raw-ingestion source, this is a FULL REFRESH every
-- run, not incremental: the old pipeline deletes and rewrites its entire
-- output folder per client every time, because this data is "entirely
-- Odoo-derived and never hand-edited" (no human-edit-protection concern,
-- no incremental-cursor complexity needed). The DB-native fetcher mirrors
-- this by deleting a client's existing task rows (cascading to events/
-- attachments) and reinserting fresh on every run, keyed by client_id.
--
-- Natural key is (client_id, odoo_task_id) -- an Odoo task id is only
-- unique within its own Odoo instance/database, not globally, so it must
-- always be paired with client_id.

CREATE TABLE implementation_tasks (
    id                  SERIAL PRIMARY KEY,
    client_id           INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    odoo_task_id         INTEGER NOT NULL,
    project_name        TEXT,
    task_name           TEXT NOT NULL,
    stage               TEXT,
    owner               TEXT,
    priority            TEXT,             -- 'High' | 'Normal'
    kanban_state        TEXT,
    active              BOOLEAN NOT NULL DEFAULT true,
    description         TEXT,
    task_created_date   DATE,
    task_updated_date   DATE,
    deadline            DATE,
    generated_at        TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (client_id, odoo_task_id)
);

CREATE INDEX idx_implementation_tasks_client_id ON implementation_tasks(client_id);
CREATE INDEX idx_implementation_tasks_stage ON implementation_tasks(stage);

-- One row per chatter entry (log note, stage change, email, other),
-- mirroring ticket_events' shape. tracking_changes holds the structured
-- field-level diffs Odoo's mail.message.message_format() surfaces for
-- stage_change events (e.g. "Stage: Requirement -> Assigned").
CREATE TABLE implementation_task_events (
    id                  SERIAL PRIMARY KEY,
    task_id             INTEGER NOT NULL REFERENCES implementation_tasks(id) ON DELETE CASCADE,
    odoo_msg_id         BIGINT,
    event_type          TEXT NOT NULL,    -- 'stage_change' | 'log_note' | 'email' | 'other'
    author               TEXT,
    event_time           TIMESTAMPTZ,
    body                 TEXT NOT NULL,
    tracking_changes     JSONB NOT NULL DEFAULT '[]',  -- [{field, old, new}, ...]
    event_order          INTEGER NOT NULL
);

CREATE INDEX idx_implementation_task_events_task_id ON implementation_task_events(task_id);

-- Metadata only, no binary content -- matches ticket_attachments' pattern
-- of a pointer rather than a blob. Odoo attachment bytes aren't fetched
-- by the DB-native fetcher (v1 scope: task/event text, not attachment
-- content); odoo_attachment_id lets a future pass fetch bytes on demand.
CREATE TABLE implementation_task_attachments (
    id                  SERIAL PRIMARY KEY,
    task_id             INTEGER NOT NULL REFERENCES implementation_tasks(id) ON DELETE CASCADE,
    odoo_attachment_id  INTEGER NOT NULL,
    filename            TEXT NOT NULL,
    mimetype            TEXT,
    size_bytes          BIGINT
);

CREATE INDEX idx_implementation_task_attachments_task_id ON implementation_task_attachments(task_id);
