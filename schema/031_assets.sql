-- New raw source category: "assets" -- curated internal reference documents
-- (SOPs, company overview, ICP, salary register, product-feature specs,
-- technical references) that raj-wiki-vault's older file-based pipeline
-- ingested from raw/assets/*.md into wiki/sources/assets/*.md wiki pages.
-- Those wiki pages were migrated into this database's wiki_pages table at
-- some earlier point (source_file_path still literally reads
-- 'wiki/sources/assets/<title>.md'), but the RAW layer never was --
-- unlike every other source category here (emails, tickets, calls,
-- implementation tasks), "assets" never got its own raw table, so those
-- 15 wiki pages' wiki_citations rows sat permanently unresolved
-- (source_type='unresolved', source_id=NULL). Found and fixed 2026-08-12
-- -- see ingestion/import_assets.py for the one-time backfill this table
-- is seeded from.
--
-- Unlike the API-fetched sources, there is no ongoing external feed for
-- this category -- these are manually-curated documents. access_tier
-- still applies (the salary register in particular is highly sensitive)
-- and is classified per-document the same way every other source
-- classifies it (ingestion/inline_tier_classifier.py), not applied
-- uniformly.
CREATE TABLE assets (
    id                  SERIAL PRIMARY KEY,
    slug                TEXT UNIQUE NOT NULL,   -- matches wiki_pages.sources_raw / wiki_citations.source_ref_raw
    title               TEXT NOT NULL,
    body                TEXT NOT NULL,
    source_file_path    TEXT,                    -- provenance: raj-wiki-vault's raw/assets/<file>, for reference only
    access_tier         access_tier NOT NULL,
    imported_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_assets_access_tier ON assets(access_tier);
