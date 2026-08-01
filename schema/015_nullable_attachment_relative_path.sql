-- email_attachments.relative_path was NOT NULL, assuming the file-based
-- loader's raw/emails/<account>/attachments/<thread_id>/<filename> layout.
-- The DB-native Gmail/Zoho fetchers record attachment METADATA only
-- (filename/size/note) -- no binary content is downloaded or written to
-- any filesystem (v1 scope, same decision as the Odoo implementation-task
-- attachments in migration 014), so there is no relative_path to give.
-- Same rationale as migration 012's source_file_path relaxation.

ALTER TABLE email_attachments ALTER COLUMN relative_path DROP NOT NULL;
