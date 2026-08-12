-- google-auth-oauthlib auto-enables PKCE (RFC 7636): Flow.authorization_url()
-- generates a code_verifier and sends its hashed code_challenge to Google;
-- the token exchange must present the SAME code_verifier back. /connect and
-- /callback are separate HTTP requests, each building its own throwaway Flow
-- object -- with nothing persisted between them, /callback's Flow has no
-- verifier at all and Google's token endpoint fails with "Missing code
-- verifier" (InvalidGrantError). The connect token row (already the thing
-- correlating the two requests via `state`) is the natural place to carry it.
ALTER TABLE oauth_connect_tokens ADD COLUMN code_verifier TEXT;
