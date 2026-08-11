-- Database-backed replacement (running alongside, not instead of, for now)
-- for claude-notes-vault's GitHub-file-based save_chat_transcript. Append-
-- only by design -- each save is a new row, never an overwrite of prior
-- content, which structurally avoids the failure mode found live in the
-- git-based version (its HTTP API path passes only the newest exchange into
-- a function that overwrites the whole file, silently dropping everything
-- before it).

CREATE TABLE threads (
    id           SERIAL PRIMARY KEY,
    username     TEXT NOT NULL,
    thread_name  TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (username, thread_name)
);

CREATE TABLE thread_messages (
    id          SERIAL PRIMARY KEY,
    thread_id   INTEGER NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
    content     TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_thread_messages_thread_id ON thread_messages(thread_id, created_at);
CREATE INDEX idx_threads_username ON threads(username);
