"""Self-serve OAuth connect flow for Gmail sources: turns "get someone's
refresh token" into "send them one link" instead of a script/terminal
session. See docs/raw-ingestion.md for the full flow and why.

CLI:
    python -m ingestion.oauth_gmail invite <account_label> <display_name> [--hours 48] [--by "Raj"]
    python -m ingestion.oauth_gmail seed-existing   # one-time backfill of raj/ron/remya from .env

FastAPI routes (mounted into ingestion/server.py):
    GET /oauth/gmail/connect?token=...             -- redirects to Google's consent screen
    GET /oauth/gmail/callback?code=...&state=...   -- exchanges code, stores refresh_token
"""
import argparse
import os
import secrets
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from google_auth_oauthlib.flow import Flow

from ingestion.db import get_live_conn
from ingestion.state import now_utc

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

router = APIRouter()


def _client_config():
    """One shared OAuth app across every connected Gmail account -- same
    client_id/secret raj_gmail/ron_gmail/remya_gmail already used, just
    under a source-neutral env var name instead of RAJ_GMAIL_*."""
    return {
        "web": {
            "client_id": os.environ["GMAIL_OAUTH_CLIENT_ID"],
            "client_secret": os.environ["GMAIL_OAUTH_CLIENT_SECRET"],
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }


def _redirect_uri():
    base = os.environ["OAUTH_REDIRECT_BASE_URL"].rstrip("/")
    return f"{base}/oauth/gmail/callback"


def generate_connect_link(account_label, display_name, created_by=None, ttl_hours=48):
    """Inserts a single-use, expiring invite token and returns the full URL
    to hand the account owner. The token itself grants no data access --
    it only starts a normal Google consent flow scoped to one account_label;
    Google is where the actual login happens."""
    token = secrets.token_urlsafe(32)
    conn = get_live_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO oauth_connect_tokens (token, account_label, display_name, expires_at, created_by)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (token, account_label, display_name, now_utc() + timedelta(hours=ttl_hours), created_by),
            )
        conn.commit()
    finally:
        conn.close()
    base = os.environ["OAUTH_REDIRECT_BASE_URL"].rstrip("/")
    return f"{base}/oauth/gmail/connect?token={token}"


def _load_connect_token(token):
    conn = get_live_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM oauth_connect_tokens WHERE token = %s", (token,))
            return cur.fetchone()
    finally:
        conn.close()


def _mark_token_used(token):
    conn = get_live_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE oauth_connect_tokens SET used_at = now() WHERE token = %s", (token,))
        conn.commit()
    finally:
        conn.close()


def _upsert_account(account_label, display_name, source_type, refresh_token, connected_by):
    conn = get_live_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO oauth_accounts (account_label, display_name, source_type, refresh_token, connected_by)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (account_label) DO UPDATE SET
                    refresh_token = EXCLUDED.refresh_token,
                    status = 'active',
                    connected_at = now(),
                    connected_by = EXCLUDED.connected_by
                """,
                (account_label, display_name, source_type, refresh_token, connected_by),
            )
        conn.commit()
    finally:
        conn.close()


@router.get("/oauth/gmail/connect")
async def gmail_connect(token: str):
    row = _load_connect_token(token)
    if row is None:
        raise HTTPException(status_code=404, detail="Invalid or unknown invite link")
    if row["used_at"] is not None:
        raise HTTPException(status_code=410, detail="This invite link has already been used")
    if row["expires_at"] < now_utc():
        raise HTTPException(status_code=410, detail="This invite link has expired -- ask for a new one")

    flow = Flow.from_client_config(_client_config(), scopes=SCOPES, redirect_uri=_redirect_uri())
    # prompt=consent forces Google to reissue a refresh_token even if this
    # person has authorized this app before -- without it, a repeat consent
    # can come back with an access_token only, and we'd store nothing.
    auth_url, _ = flow.authorization_url(access_type="offline", prompt="consent", state=token)
    return RedirectResponse(auth_url)


@router.get("/oauth/gmail/callback")
async def gmail_callback(request: Request, code: str | None = None, state: str | None = None, error: str | None = None):
    if error:
        return HTMLResponse(f"<p>Google reported an error: {error}. Nothing was connected -- ask for a new link.</p>", status_code=400)
    if not code or not state:
        raise HTTPException(status_code=400, detail="Missing code/state")

    row = _load_connect_token(state)
    if row is None or row["used_at"] is not None or row["expires_at"] < now_utc():
        raise HTTPException(status_code=410, detail="This invite link is no longer valid -- ask for a new one")

    flow = Flow.from_client_config(_client_config(), scopes=SCOPES, redirect_uri=_redirect_uri())
    flow.fetch_token(code=code)
    refresh_token = flow.credentials.refresh_token
    if not refresh_token:
        # Should be unreachable given prompt=consent above -- fail loud
        # rather than silently storing nothing.
        return HTMLResponse(
            "<p>Google didn't return a refresh token. Revoke this app's access at "
            "myaccount.google.com/permissions and try the link again.</p>", status_code=400,
        )

    _upsert_account(row["account_label"], row["display_name"], row["source_type"], refresh_token, connected_by=row["created_by"])
    _mark_token_used(state)

    return HTMLResponse(f"<p>{row['display_name']}'s Gmail is connected. You can close this tab.</p>")


def seed_existing_accounts():
    """One-time backfill: moves raj_gmail/ron_gmail/remya_gmail's refresh
    tokens out of .env and into oauth_accounts, so gmail_fetcher.py's
    switch to DB-backed account loading doesn't disrupt the 3 accounts
    already in production. Safe to re-run (ON CONFLICT upsert)."""
    legacy = {
        "raj_gmail": ("Raj", "RAJ_GMAIL", True),
        "ron_gmail": ("Ron", "RON_GMAIL", True),
        "remya_gmail": ("Remya", "REMYA_GMAIL", False),  # 2026-08-10: one-time historical pull only
    }
    conn = get_live_conn()
    try:
        with conn.cursor() as cur:
            for label, (display_name, prefix, raw_sweep_enabled) in legacy.items():
                refresh_token = os.environ.get(f"{prefix}_REFRESH_TOKEN")
                if not refresh_token:
                    print(f"skip {label}: no {prefix}_REFRESH_TOKEN in .env")
                    continue
                cur.execute(
                    """
                    INSERT INTO oauth_accounts (account_label, display_name, source_type, refresh_token, raw_sweep_enabled, connected_by)
                    VALUES (%s, %s, 'gmail', %s, %s, 'seed-existing')
                    ON CONFLICT (account_label) DO UPDATE SET
                        refresh_token = EXCLUDED.refresh_token,
                        raw_sweep_enabled = EXCLUDED.raw_sweep_enabled
                    """,
                    (label, display_name, refresh_token, raw_sweep_enabled),
                )
                print(f"seeded {label} (raw_sweep_enabled={raw_sweep_enabled})")
        conn.commit()
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)

    invite = sub.add_parser("invite", help="Generate a one-time connect link for a new account")
    invite.add_argument("account_label", help="e.g. isha_gmail")
    invite.add_argument("display_name", help="e.g. Isha")
    invite.add_argument("--hours", type=int, default=48)
    invite.add_argument("--by", default=None, help="who generated this link, for the audit trail")

    sub.add_parser("seed-existing", help="Backfill raj/ron/remya from .env into oauth_accounts")

    args = parser.parse_args()

    if args.cmd == "invite":
        url = generate_connect_link(args.account_label, args.display_name, created_by=args.by, ttl_hours=args.hours)
        print(url)
    elif args.cmd == "seed-existing":
        seed_existing_accounts()


if __name__ == "__main__":
    main()
