-- Connected-account credential store for OAuth-based raw sources (Gmail
-- today; source_type is generic so Zoho/other OAuth sources can reuse this
-- later instead of another per-source table). Replaces the old pattern of
-- one hardcoded {PREFIX}_CLIENT_ID/SECRET/REFRESH_TOKEN triplet per account
-- in .env -- refresh_token now lives here so a newly-connected account is
-- picked up by the next sweep with no .env edit and no service restart.
-- client_id/client_secret are NOT per-row: Gmail accounts share one OAuth
-- app (GMAIL_OAUTH_CLIENT_ID/SECRET in .env), same as the raj/ron/remya
-- setup this table replaces.
--
-- status is a soft-delete flag (never a real DELETE), same convention as
-- employees.status (schema/025_employees.sql) -- 'revoked' keeps the row
-- (and its audit trail) instead of erasing history.
--
-- raw_sweep_enabled is a SEPARATE flag from status, not folded into it:
-- an account can be status='active' (still a legitimate, connected source
-- that wiki-ingestion should keep treating as a known category) while
-- raw_sweep_enabled=false (no new raw fetches -- e.g. remya_gmail, which
-- was a deliberate one-time historical pull, never an ongoing source; see
-- docs/raw-ingestion.md). Collapsing these into one status value would
-- lose that distinction.
CREATE TABLE oauth_accounts (
    id                  SERIAL PRIMARY KEY,
    account_label       TEXT UNIQUE NOT NULL,   -- e.g. 'isha_gmail' -- matches source_account in email_threads
    display_name        TEXT NOT NULL,          -- e.g. 'Isha'
    source_type         TEXT NOT NULL DEFAULT 'gmail',
    refresh_token       TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'revoked')),
    raw_sweep_enabled   BOOLEAN NOT NULL DEFAULT true,
    connected_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    connected_by        TEXT
);

-- Single-use, expiring invite links for the self-serve OAuth connect flow
-- (ingestion/oauth_gmail.py). A row here is NOT a credential -- it's a
-- capability to start the Google consent flow for one specific
-- account_label, valid until expires_at, consumed (used_at set) on first
-- successful callback. Deliberately its own table rather than a signed
-- stateless token so a link can be individually revoked (DELETE the row)
-- and so who-generated-what stays auditable, matching this codebase's
-- existing audit-log conventions (employee_change_log, ingest_log).
CREATE TABLE oauth_connect_tokens (
    token           TEXT PRIMARY KEY,
    account_label   TEXT NOT NULL,
    display_name    TEXT NOT NULL,
    source_type     TEXT NOT NULL DEFAULT 'gmail',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by      TEXT,
    expires_at      TIMESTAMPTZ NOT NULL,
    used_at         TIMESTAMPTZ
);
