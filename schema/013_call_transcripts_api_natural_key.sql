-- Migration 012 made call_transcripts.source_file_path nullable for
-- API-direct fetchers (Fireflies/Fathom), but the table's only unique
-- constraint is UNIQUE (source, external_id, source_file_path) -- and
-- Postgres treats NULL as distinct from any other NULL in a unique
-- constraint, so ON CONFLICT on that triple never matches two API-fetched
-- rows for the same call. Every re-run of an API fetcher would insert a
-- fresh duplicate row instead of upserting. This was flagged as a known
-- risk in migration 012's own comment ("NOT YET HIT IN PRACTICE") -- hit
-- while building fireflies_fetcher.py's write layer.
--
-- Fix: a separate partial unique index covering ONLY the NULL-path
-- (API-fetched) rows, so those can use (source, external_id) as their
-- natural key via ON CONFLICT ... WHERE source_file_path IS NULL. The
-- original 3-column constraint is untouched and keeps governing
-- file-based rows.

CREATE UNIQUE INDEX idx_call_transcripts_api_natural_key
    ON call_transcripts (source, external_id)
    WHERE source_file_path IS NULL;
