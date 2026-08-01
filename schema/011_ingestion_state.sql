-- State tables for the DB-native raw-ingestion service (replaces the
-- Render pipeline's SQLite PipelineState: contacts.last_*_sync_at cursors
-- and the message_ids cross-thread dedup table).

CREATE TABLE sync_cursors (
    source          TEXT PRIMARY KEY,   -- 'raj_gmail' | 'ron_gmail' | 'remya_gmail' |
                                         -- 'support_zoho' | 'fireflies' | 'fathom' |
                                         -- 'odoo_<client_id>' (e.g. 'odoo_greer')
    last_synced_at  TIMESTAMPTZ,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Cross-thread message-level dedup: a message already recorded under one
-- thread should not cause a second thread to be (re)written if the same
-- message shows up there too (mirrors PipelineState.any_messages_already_processed).
CREATE TABLE message_ids_seen (
    message_id      TEXT PRIMARY KEY,
    thread_source_account TEXT NOT NULL,
    thread_id       INTEGER,             -- references email_threads(id), nullable during dry-run
    seen_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_message_ids_seen_thread ON message_ids_seen(thread_id);
