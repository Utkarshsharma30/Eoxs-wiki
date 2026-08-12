-- The self-serve OAuth connect flow (ingestion/oauth_gmail.py) needs a
-- Google "Web application" OAuth client (arbitrary HTTPS redirect_uri) --
-- the "Desktop app" client raj_gmail/ron_gmail/remya_gmail were originally
-- connected with (GMAIL_OAUTH_CLIENT_ID/SECRET) cannot register a custom
-- redirect_uri at all, Google restricts Desktop clients to localhost/OOB.
-- Refreshing an already-issued token never re-validates redirect_uri, so
-- the 3 existing accounts are unaffected and keep using the Desktop
-- client forever -- but a refresh_token IS bound to whichever client
-- minted it, so gmail_fetcher.py needs to know, per account, which
-- client_id/secret pair to refresh with.
ALTER TABLE oauth_accounts
    ADD COLUMN client_type TEXT NOT NULL DEFAULT 'web'
        CHECK (client_type IN ('legacy_desktop', 'web'));

UPDATE oauth_accounts SET client_type = 'legacy_desktop'
    WHERE account_label IN ('raj_gmail', 'ron_gmail', 'remya_gmail');
