-- Raw-ingestion (DB-native, API-fetched) writes have no filesystem path.
-- The real natural key for these tables is already a separate UNIQUE
-- constraint (email_threads: source_account+gmail_thread_id; tickets:
-- ticket_number; sales_orders: order_number; call_transcripts:
-- source+external_id+source_file_path). Relax source_file_path to nullable
-- so API-written rows don't need a synthetic value at all, while file-based
-- loaders (still used for local/offline work) keep working unchanged.
-- wiki_pages is intentionally untouched: source_file_path is its real
-- natural key until the wiki-ingestion DB-native redesign lands.

ALTER TABLE email_threads ALTER COLUMN source_file_path DROP NOT NULL;
ALTER TABLE tickets ALTER COLUMN source_file_path DROP NOT NULL;
ALTER TABLE sales_orders ALTER COLUMN source_file_path DROP NOT NULL;
ALTER TABLE call_transcripts ALTER COLUMN source_file_path DROP NOT NULL;
