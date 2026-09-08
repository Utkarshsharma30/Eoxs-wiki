-- Real full-text search for implementation_tasks. Previously
-- search_implementation_tasks used ILIKE '%whole query string%' against
-- task_name/description -- strictly worse than word-level AND, since it
-- required the entire multi-word query to appear literally, in order, as a
-- substring. Safe to add now that ingestion/write_implementation.py upserts
-- on (client_id, odoo_task_id) instead of the full DELETE+INSERT this table
-- used before -- id and any derived/stored column survive a refresh, so a
-- generated STORED column (same pattern as email_messages.body_tsv,
-- call_transcripts.transcript_tsv) works cleanly here.
ALTER TABLE implementation_tasks
    ADD COLUMN task_tsv TSVECTOR GENERATED ALWAYS AS (
        to_tsvector('english', coalesce(task_name, '') || ' ' || coalesce(description, ''))
    ) STORED;

CREATE INDEX idx_implementation_tasks_tsv ON implementation_tasks USING GIN (task_tsv);
