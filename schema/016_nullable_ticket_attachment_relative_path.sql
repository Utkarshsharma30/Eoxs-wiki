-- Same rationale as migration 015 (email_attachments): the DB-native
-- tickets fetcher records attachment METADATA only (filename/mimetype/
-- size), no binary content and no filesystem path -- matching the v1
-- scope decision already made for Odoo implementation-task attachments
-- (migration 014) and email attachments (migration 015).

ALTER TABLE ticket_attachments ALTER COLUMN relative_path DROP NOT NULL;
ALTER TABLE ticket_attachments ADD COLUMN IF NOT EXISTS mimetype TEXT;
ALTER TABLE ticket_attachments ADD COLUMN IF NOT EXISTS size_bytes BIGINT;
