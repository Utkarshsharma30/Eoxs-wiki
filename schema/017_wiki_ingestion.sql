-- Wiki-ingestion DB-native rewrite, Phase 1: staging area + cycle/batch
-- tracking + content-hash dedup. Replaces wiki-agent's git
-- worktrees/branches with a Postgres schema; replaces its SQLite
-- state.db with these tables.
--
-- Staging lives in a SEPARATE SCHEMA within eoxs_wiki itself (not the
-- eoxs_wiki_staging DATABASE, which stays reserved for raw-ingestion's
-- dual-write testing, unrelated to this). Reason: Postgres has no atomic
-- cross-database transactions, so promoting staging->live would need
-- replication/dblink/manual export if staging lived in a different
-- database. Same-database, different-schema makes promotion a single
-- atomic transaction (INSERT...SELECT + DELETE, or UPDATE for an
-- existing live page). Applied to both databases for schema parity with
-- every other migration, even though wiki_staging usage in practice
-- targets eoxs_wiki.

CREATE SCHEMA IF NOT EXISTS wiki_staging;

-- Draft wiki pages pending review/promotion. live_page_id is NULL for a
-- brand-new page (promotion = INSERT into public.wiki_pages); non-NULL
-- means this draft proposes updating that existing live page (promotion
-- = UPDATE). No source_file_path/mtime here at all -- unlike
-- public.wiki_pages, staging drafts are DB-native from day one, no file
-- concept applies.
CREATE TABLE wiki_staging.wiki_pages (
    id                  SERIAL PRIMARY KEY,
    live_page_id        INTEGER,             -- REFERENCES public.wiki_pages(id), no FK (cross-schema, live row may not exist yet at draft time)
    title               TEXT NOT NULL,
    page_type           public.wiki_page_type NOT NULL,
    entity_class        TEXT,
    tags                TEXT[] NOT NULL DEFAULT '{}',
    sources_raw         TEXT[] NOT NULL DEFAULT '{}',
    created_date        DATE,
    updated_date        DATE,
    updated_raw         TEXT,
    generated_hash      TEXT,
    body                TEXT NOT NULL,
    source_kind         TEXT NOT NULL,       -- which partition wrote this draft, e.g. 'raj_gmail', 'tickets'
    cycle_id            INTEGER NOT NULL,    -- REFERENCES public.wiki_ingest_cycles(id), see below
    status              TEXT NOT NULL DEFAULT 'draft',  -- 'draft' | 'reviewed' | 'promoted' | 'rejected'
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_wiki_staging_pages_title ON wiki_staging.wiki_pages(title);
CREATE INDEX idx_wiki_staging_pages_cycle ON wiki_staging.wiki_pages(cycle_id);
CREATE INDEX idx_wiki_staging_pages_status ON wiki_staging.wiki_pages(status);

CREATE TABLE wiki_staging.wiki_links (
    id                SERIAL PRIMARY KEY,
    from_page_id      INTEGER NOT NULL REFERENCES wiki_staging.wiki_pages(id) ON DELETE CASCADE,
    to_page_id        INTEGER,   -- may resolve to a wiki_staging.wiki_pages(id) OR a public.wiki_pages(id) at promotion time
    to_title_raw      TEXT NOT NULL,
    display_text      TEXT,
    context_snippet   TEXT
);

CREATE INDEX idx_wiki_staging_links_from ON wiki_staging.wiki_links(from_page_id);

CREATE TABLE wiki_staging.wiki_citations (
    id              SERIAL PRIMARY KEY,
    wiki_page_id    INTEGER NOT NULL REFERENCES wiki_staging.wiki_pages(id) ON DELETE CASCADE,
    source_type     TEXT NOT NULL,
    source_id       INTEGER,
    source_ref_raw  TEXT NOT NULL
);

CREATE INDEX idx_wiki_staging_citations_page ON wiki_staging.wiki_citations(wiki_page_id);

CREATE TABLE wiki_staging.wiki_flags (
    id              SERIAL PRIMARY KEY,
    wiki_page_id    INTEGER NOT NULL REFERENCES wiki_staging.wiki_pages(id) ON DELETE CASCADE,
    flag_type       public.wiki_flag_type NOT NULL,
    text            TEXT NOT NULL
);

CREATE INDEX idx_wiki_staging_flags_page ON wiki_staging.wiki_flags(wiki_page_id);

-- Operational tracking, mirroring wiki-agent's SQLite state.db (cycle/
-- issue tables) in Postgres. Lives in public alongside ingest_log/
-- sync_cursors/message_ids_seen, matching existing naming (wiki_ingest_ prefix).

CREATE TABLE wiki_ingest_cycles (
    id                      SERIAL PRIMARY KEY,
    started_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at             TIMESTAMPTZ,
    status                  TEXT NOT NULL DEFAULT 'running',  -- 'running' | 'done' | 'failed'
    summary                 JSONB,
    linear_parent_issue_id  TEXT
);

CREATE TABLE wiki_ingest_batches (
    id              SERIAL PRIMARY KEY,
    cycle_id        INTEGER NOT NULL REFERENCES wiki_ingest_cycles(id) ON DELETE CASCADE,
    source_kind     TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'running',  -- 'running' | 'done' | 'ingest_failed'
    row_count       INTEGER NOT NULL DEFAULT 0,
    linear_issue_id TEXT,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at     TIMESTAMPTZ,
    error           TEXT
);

CREATE INDEX idx_wiki_ingest_batches_cycle ON wiki_ingest_batches(cycle_id);

-- Content-hash dedup, mirroring wiki-agent's seen_file(path, content_digest).
-- Needed because implementation_tasks (and potentially other sources) do a
-- full delete+reinsert on every raw-ingestion sweep -- without this, every
-- row would look "changed" (new updated_at) on every wiki-ingestion cycle
-- even when its actual content is identical, wasting real LLM cost
-- reprocessing unchanged data every 2 hours.
CREATE TABLE wiki_ingest_seen (
    source_kind     TEXT NOT NULL,
    source_row_id   INTEGER NOT NULL,
    content_hash    TEXT NOT NULL,
    decision        TEXT NOT NULL,   -- 'processed' | 'skipped_unchanged' | 'skipped_noise'
    last_seen_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    cycle_id        INTEGER REFERENCES wiki_ingest_cycles(id) ON DELETE SET NULL,
    PRIMARY KEY (source_kind, source_row_id)
);
