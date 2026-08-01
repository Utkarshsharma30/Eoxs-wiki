-- Wiki pages and their relational replicas of the [[wikilink]] graph,
-- source citations, and flagged callouts (contradiction/unverified).
-- Source: wiki/{entities,concepts,sources,analyses,overviews,prospects}/**/*.md

CREATE TYPE wiki_page_type AS ENUM ('entity', 'concept', 'source', 'analysis', 'overview', 'prospect');

CREATE TABLE wiki_pages (
    id                  SERIAL PRIMARY KEY,
    title               TEXT NOT NULL,              -- frontmatter 'title', also used for [[wikilink]] resolution
    page_type           wiki_page_type NOT NULL,
    entity_class        TEXT,                        -- e.g. 'person' — only set when page_type = 'entity'
    tags                TEXT[] NOT NULL DEFAULT '{}',
    sources_raw         TEXT[] NOT NULL DEFAULT '{}', -- frontmatter 'sources' list, as written (slugs/filenames)
    created_date        DATE,
    updated_date        DATE,
    generated_hash      TEXT,
    body                TEXT NOT NULL,
    body_tsv            TSVECTOR GENERATED ALWAYS AS (to_tsvector('english', coalesce(body, ''))) STORED,
    source_file_path    TEXT NOT NULL UNIQUE,        -- relative path under VAULT_ROOT/wiki/, natural key
    source_file_mtime   TIMESTAMPTZ NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Exact-filename match is how Obsidian resolves [[wikilinks]] (per CLAUDE.md convention).
-- NOT unique: the vault itself has a handful of pre-existing duplicate titles at
-- different paths (e.g. entities/contacts/Jamie Hansen.md vs entities/external/Jamie Hansen.md) —
-- an existing ambiguity in the vault's own wikilink resolution, not something to mask here.
CREATE INDEX idx_wiki_pages_title ON wiki_pages(title);
CREATE INDEX idx_wiki_pages_type ON wiki_pages(page_type);
CREATE INDEX idx_wiki_pages_tsv ON wiki_pages USING gin (body_tsv);
CREATE INDEX idx_wiki_pages_title_trgm ON wiki_pages USING gin (title gin_trgm_ops);

-- Replica of the [[wikilink]] graph. to_page_id is NULL for unresolved links
-- (page doesn't exist yet) — per CLAUDE.md, "a cross-reference that doesn't
-- exist yet is a placeholder, not an error," so we keep it rather than drop it.
CREATE TABLE wiki_links (
    id                SERIAL PRIMARY KEY,
    from_page_id      INTEGER NOT NULL REFERENCES wiki_pages(id) ON DELETE CASCADE,
    to_page_id        INTEGER REFERENCES wiki_pages(id) ON DELETE CASCADE,
    to_title_raw      TEXT NOT NULL,   -- the literal [[Title]] or [[Title|display]] target text
    display_text      TEXT,             -- text after the pipe, if present
    context_snippet   TEXT              -- surrounding sentence, for readability when browsing the graph
);

CREATE INDEX idx_wiki_links_from ON wiki_links(from_page_id);
CREATE INDEX idx_wiki_links_to ON wiki_links(to_page_id);
CREATE INDEX idx_wiki_links_unresolved ON wiki_links(to_title_raw) WHERE to_page_id IS NULL;

-- Polymorphic citation: a wiki page's frontmatter 'sources' entry pointing
-- at a raw source row. source_type names the target table informally;
-- source_id is that table's primary key. No DB-level FK since the target
-- table varies (emails, tickets, calls, etc. loaded in later passes).
CREATE TABLE wiki_citations (
    id              SERIAL PRIMARY KEY,
    wiki_page_id    INTEGER NOT NULL REFERENCES wiki_pages(id) ON DELETE CASCADE,
    source_type     TEXT NOT NULL,     -- e.g. 'email_thread', 'ticket', 'call_transcript'
    source_id       INTEGER,           -- nullable: unresolved citation (source not loaded/found yet)
    source_ref_raw  TEXT NOT NULL      -- the literal string from frontmatter 'sources', for traceability
);

CREATE INDEX idx_wiki_citations_page ON wiki_citations(wiki_page_id);
CREATE INDEX idx_wiki_citations_source ON wiki_citations(source_type, source_id);

CREATE TYPE wiki_flag_type AS ENUM ('contradiction', 'unverified');

CREATE TABLE wiki_flags (
    id              SERIAL PRIMARY KEY,
    wiki_page_id    INTEGER NOT NULL REFERENCES wiki_pages(id) ON DELETE CASCADE,
    flag_type       wiki_flag_type NOT NULL,
    text            TEXT NOT NULL     -- the callout content following the ⚠️/🔍 marker
);

CREATE INDEX idx_wiki_flags_page ON wiki_flags(wiki_page_id);
