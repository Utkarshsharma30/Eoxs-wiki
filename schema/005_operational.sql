-- Operational tables: ingest log replica and incremental-sync state.

CREATE TABLE ingest_log (
    id              SERIAL PRIMARY KEY,
    log_date        DATE NOT NULL,
    operation       TEXT NOT NULL,     -- ingest | query | lint | create | update
    description     TEXT NOT NULL,
    raw_entry       TEXT NOT NULL      -- full markdown entry text, for traceability back to log.md
);

-- Tracks, per source file, the mtime/hash last loaded — lets loaders skip
-- unchanged files on rerun instead of reparsing the whole vault every time.
CREATE TABLE db_sync_state (
    source_type       TEXT NOT NULL,      -- e.g. 'email_thread', 'wiki_page'
    source_file_path  TEXT NOT NULL,      -- relative path under VAULT_ROOT
    file_mtime        TIMESTAMPTZ NOT NULL,
    file_hash         TEXT,
    last_loaded_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (source_type, source_file_path)
);
