-- Recreates the migration that added attachment text-extraction columns to
-- email_attachments (applied live 2026-08-07 15:24:50 as this exact
-- filename per schema_migrations, but the file itself was lost from disk
-- before being committed -- this restores the repo's schema/ directory to
-- match deployed reality; IF NOT EXISTS makes it a safe no-op wherever the
-- columns already exist).
--
-- source_attachment_id: the provider's own attachment id (Gmail
-- body.attachmentId, Zoho attachmentId) -- needed to re-fetch a specific
-- attachment's bytes on demand, since only metadata was captured at
-- original ingestion time (see migration 015's comment).
-- mimetype: the provider-reported content type.
-- extracted_text: text pulled from extractable attachment types (PDF,
-- DOCX, XLSX, plain text/CSV) via ingestion/backfill_attachments.py --
-- NULL for non-extractable types (images, calendar invites, archives,
-- etc.), which still get source_attachment_id/mimetype filled.

ALTER TABLE email_attachments ADD COLUMN IF NOT EXISTS source_attachment_id TEXT;
ALTER TABLE email_attachments ADD COLUMN IF NOT EXISTS mimetype TEXT;
ALTER TABLE email_attachments ADD COLUMN IF NOT EXISTS extracted_text TEXT;
