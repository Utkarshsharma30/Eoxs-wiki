-- New raw source category: "repo_docs" -- this repository's own docs, the
-- architecture overview, and a synthesized codebase overview, made
-- queryable through MCP the same way every other source is (2026-08-26).
-- Explicit policy: every row here is tier1 (Raj-only, FULL_CLEARANCE) --
-- this is internal engineering/ops detail about how the system itself
-- works (credentials, infra topology, schema internals, redaction logic),
-- not something hr/general/intern identities have any reason to see.
-- Unlike `assets` (schema/031), access_tier is NOT classified per-document
-- here -- every row is hardcoded 'tier1' at import time, never computed.
--
-- Like `assets`, there is no ongoing external feed for this category --
-- these are files already living in this repo (docs/*.md, ARCHITECTURE.md,
-- CLAUDE.md) plus one synthesized codebase-overview document with no
-- single source file of its own. See ingestion/import_repo_docs.py for the
-- (re-runnable, upserting) import script this table is seeded from.
CREATE TABLE repo_docs (
    id                  SERIAL PRIMARY KEY,
    slug                TEXT UNIQUE NOT NULL,
    title               TEXT NOT NULL,
    body                TEXT NOT NULL,
    doc_type            TEXT NOT NULL,   -- 'doc' | 'architecture' | 'codebase'
    source_file_path    TEXT,             -- provenance: repo-relative path, for reference only (NULL for the synthesized codebase overview)
    access_tier         access_tier NOT NULL DEFAULT 'tier1',
    imported_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_repo_docs_access_tier ON repo_docs(access_tier);
CREATE INDEX idx_repo_docs_title_trgm ON repo_docs USING gin (title gin_trgm_ops);
