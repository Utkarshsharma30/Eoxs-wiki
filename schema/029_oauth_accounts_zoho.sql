-- Generalizing oauth_accounts (originally Gmail-only, schema/026) to also
-- carry Zoho Mail accounts. Zoho's API scopes every call under
-- /accounts/{account_id}/ -- a numeric per-mailbox identifier Zoho itself
-- assigns, with no Gmail equivalent (Gmail's API implicitly scopes to
-- "me" via the token). Nullable: only source_type='zoho' rows use it.
ALTER TABLE oauth_accounts ADD COLUMN external_account_id TEXT;

-- schema/027's client_type CHECK only allowed Gmail's two literal values
-- ('legacy_desktop'/'web'). Relaxed to free text -- the exact vocabulary of
-- "which registered OAuth client does this account's refresh_token belong
-- to" is provider-specific and will keep growing as more source_types are
-- added here; a rigid enum would need a migration every time. Convention:
-- 'legacy_<something>' for an account whose credentials predate this
-- table, 'web' for anything connected via a self-serve connect flow.
ALTER TABLE oauth_accounts DROP CONSTRAINT oauth_accounts_client_type_check;

-- The existing single hardcoded Zoho account (zoho_fetcher.py's SOURCE =
-- "support_zoho", previously ZOHO_CLIENT_ID/SECRET/REFRESH_TOKEN +
-- ZOHO_ACCOUNT_ID as fixed module globals) moves into this table the same
-- way raj_gmail/ron_gmail/remya_gmail did in schema/026 -- see
-- ingestion/oauth_zoho.py's seed_existing_account(). client_type follows
-- the same 'legacy_desktop'/'web' vocabulary schema/027 introduced for
-- Gmail, reused here for whatever the existing Zoho client's type turns
-- out to be vs. the self-serve flow's client.
