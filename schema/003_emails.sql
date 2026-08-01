-- Email threads (raj_gmail, ron_gmail, remya_gmail, support_zoho — unified),
-- their per-message bodies, and attachments.
-- Source: raw/emails/<account>/<YYYY-MM>/*.md and raw/_spam_quarantine/<account>/*.md

CREATE TYPE email_source_account AS ENUM ('raj_gmail', 'ron_gmail', 'remya_gmail', 'support_zoho');

CREATE TABLE email_threads (
    id                  SERIAL PRIMARY KEY,
    source_account      email_source_account NOT NULL,
    gmail_thread_id     TEXT NOT NULL,              -- real gmail id, or synthetic 'zoho-<epoch>' for support_zoho
    subject             TEXT,
    from_addr           TEXT,
    to_addr             TEXT,
    message_count       INTEGER,
    participants        TEXT[] NOT NULL DEFAULT '{}',
    thread_dates        TIMESTAMPTZ[] NOT NULL DEFAULT '{}', -- from frontmatter 'dates' list
    tags                TEXT[] NOT NULL DEFAULT '{}',
    is_quarantined      BOOLEAN NOT NULL DEFAULT false,      -- true if sourced from raw/_spam_quarantine/
    generated_at        TIMESTAMPTZ,
    generated_hash      TEXT,
    source_file_path    TEXT NOT NULL,              -- relative path under VAULT_ROOT, for traceability
    source_file_mtime   TIMESTAMPTZ NOT NULL,        -- for incremental sync
    client_id           INTEGER REFERENCES clients(id) ON DELETE SET NULL, -- best-effort match, nullable
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source_account, gmail_thread_id)
);

CREATE INDEX idx_email_threads_client_id ON email_threads(client_id);
CREATE INDEX idx_email_threads_subject_trgm ON email_threads USING gin (subject gin_trgm_ops);

CREATE TABLE email_messages (
    id              SERIAL PRIMARY KEY,
    thread_id       INTEGER NOT NULL REFERENCES email_threads(id) ON DELETE CASCADE,
    message_index   INTEGER NOT NULL,   -- the 'N' in '## Message N'
    message_date    TIMESTAMPTZ,
    from_addr       TEXT,
    body            TEXT NOT NULL,
    body_tsv        TSVECTOR GENERATED ALWAYS AS (to_tsvector('english', coalesce(body, ''))) STORED,
    UNIQUE (thread_id, message_index)
);

CREATE INDEX idx_email_messages_tsv ON email_messages USING gin (body_tsv);
CREATE INDEX idx_email_messages_thread_id ON email_messages(thread_id);

CREATE TABLE email_attachments (
    id              SERIAL PRIMARY KEY,
    thread_id       INTEGER NOT NULL REFERENCES email_threads(id) ON DELETE CASCADE,
    message_id      INTEGER REFERENCES email_messages(id) ON DELETE SET NULL,
    filename        TEXT NOT NULL,
    relative_path   TEXT NOT NULL,   -- raw/emails/<account>/attachments/<gmail_thread_id>/<filename>
    size_bytes      BIGINT,
    note            TEXT              -- e.g. 'duplicate of X', 'inline image, not stored'
);

CREATE INDEX idx_email_attachments_thread_id ON email_attachments(thread_id);
