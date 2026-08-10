-- Adds attachment content-extraction support to email_attachments. Two
-- real gaps this closes, found investigating why Cruz couldn't answer
-- questions about attachment content:
--   1. The native provider attachment id (Gmail's attachmentId, Zoho's
--      attachmentId) was never being captured anywhere -- fetched from
--      the API response and then discarded, only ever used to build a
--      fallback display name. Without it, an already-ingested
--      attachment can never be re-fetched later; recovering it requires
--      a full re-scan, not a simple backfill. Capturing it going
--      forward means this gap can never reopen for new attachments.
--   2. No column existed to hold extracted text at all -- attachment
--      content was structurally invisible to every MCP tool, no matter
--      what got built on top.
-- mimetype: email_attachments never had this (ticket_attachments always
-- did) -- added for parity and to help future extraction-format
-- decisions without needing to guess from the filename extension alone.
ALTER TABLE email_attachments ADD COLUMN source_attachment_id TEXT;
ALTER TABLE email_attachments ADD COLUMN mimetype TEXT;
ALTER TABLE email_attachments ADD COLUMN extracted_text TEXT;
