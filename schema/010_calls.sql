-- Call transcripts (Fireflies + Fathom, unified). Source:
--   raw/calls/<YYYY-MM>/*.md          (fireflies, unmatched to a client)
--   raw/clients/<client>/calls/*.md   (fireflies, matched to a client)
--   raw/fathom/<YYYY-MM>/*.md         (fathom, no client routing)

CREATE TYPE call_source AS ENUM ('fireflies', 'fathom');

CREATE TABLE call_transcripts (
    id                  SERIAL PRIMARY KEY,
    source              call_source NOT NULL,
    external_id         TEXT NOT NULL,   -- fireflies_id or fathom_recording_id
    meeting_title       TEXT,
    call_date           DATE,
    duration_seconds    BIGINT,
    duration_human      TEXT,
    host_email          TEXT,
    participants        TEXT[] NOT NULL DEFAULT '{}',
    recording_url       TEXT,
    fireflies_summary   TEXT,             -- fireflies only
    key_topics          TEXT[] NOT NULL DEFAULT '{}',  -- fireflies only
    action_items        TEXT[] NOT NULL DEFAULT '{}',  -- fireflies only
    tags                TEXT[] NOT NULL DEFAULT '{}',
    generated_at        TIMESTAMPTZ,
    generated_hash      TEXT,
    transcript_body     TEXT NOT NULL,
    transcript_tsv      TSVECTOR GENERATED ALWAYS AS (to_tsvector('english', coalesce(transcript_body, ''))) STORED,
    client_id           INTEGER REFERENCES clients(id) ON DELETE SET NULL, -- set when routed under raw/clients/<x>/calls/
    source_file_path    TEXT NOT NULL UNIQUE,
    source_file_mtime   TIMESTAMPTZ NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source, external_id, source_file_path)
);

CREATE INDEX idx_call_transcripts_client_id ON call_transcripts(client_id);
CREATE INDEX idx_call_transcripts_tsv ON call_transcripts USING gin (transcript_tsv);
CREATE INDEX idx_call_transcripts_date ON call_transcripts(call_date);

-- Per-speaker turn, parsed from '**Speaker:** text' lines in the transcript.
CREATE TABLE call_segments (
    id              SERIAL PRIMARY KEY,
    call_id         INTEGER NOT NULL REFERENCES call_transcripts(id) ON DELETE CASCADE,
    segment_order   INTEGER NOT NULL,
    speaker         TEXT,       -- may be unlabeled ('Speaker 1') for fireflies calls without name resolution
    text            TEXT NOT NULL
);

CREATE INDEX idx_call_segments_call_id ON call_segments(call_id);
