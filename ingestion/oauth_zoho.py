"""Self-serve OAuth connect flow for Zoho Mail sources -- same shape as
ingestion/oauth_gmail.py (send a link, they log into Zoho directly, we
never see a password), hand-rolled with httpx instead of a client library
since there's no Zoho equivalent of google-auth-oauthlib. Two things
differ from the Gmail version:

1. Zoho's API scopes every call under /accounts/{account_id}/ -- a numeric
   per-mailbox identifier with no Gmail equivalent. Auto-discovered here
   right after the token exchange (GET /api/accounts) so nobody has to
   look it up or paste it in manually -- same "zero manual steps" bar as
   the rest of this flow.
2. No PKCE -- google_auth_oauthlib enables it automatically; this hand-
   rolled version doesn't add it since Zoho's Server-based Applications
   don't require it for a confidential (client_secret-holding) client.

CLI:
    python -m ingestion.oauth_zoho invite <account_label> <display_name> [--hours 48] [--by "Raj"]
    python -m ingestion.oauth_zoho seed-existing   # one-time backfill of support_zoho from .env

FastAPI routes (mounted into ingestion/server.py):
    GET /oauth/zoho/connect?token=...             -- redirects to Zoho's consent screen
    GET /oauth/zoho/callback?code=...&state=...   -- exchanges code, discovers account_id, stores both
"""
import argparse
import os
import secrets
import sys
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ingestion.db import get_live_conn
from ingestion.state import now_utc

AUTH_URL = "https://accounts.zoho.com/oauth/v2/auth"
TOKEN_URL = "https://accounts.zoho.com/oauth/v2/token"
ACCOUNTS_API = "https://mail.zoho.com/api/accounts"
SCOPE = "ZohoMail.messages.READ,ZohoMail.accounts.READ"

router = APIRouter()


def _redirect_uri():
    base = os.environ["OAUTH_REDIRECT_BASE_URL"].rstrip("/")
    return f"{base}/oauth/zoho/callback"


def generate_connect_link(account_label, display_name, created_by=None, ttl_hours=48):
    token = secrets.token_urlsafe(32)
    conn = get_live_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO oauth_connect_tokens (token, account_label, display_name, source_type, expires_at, created_by)
                VALUES (%s, %s, %s, 'zoho', %s, %s)
                """,
                (token, account_label, display_name, now_utc() + timedelta(hours=ttl_hours), created_by),
            )
        conn.commit()
    finally:
        conn.close()
    base = os.environ["OAUTH_REDIRECT_BASE_URL"].rstrip("/")
    return f"{base}/oauth/zoho/connect?token={token}"


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


def _upsert_account(account_label, display_name, refresh_token, external_account_id, connected_by):
    conn = get_live_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO oauth_accounts (account_label, display_name, source_type, refresh_token, external_account_id, client_type, connected_by)
                VALUES (%s, %s, 'zoho', %s, %s, 'web', %s)
                ON CONFLICT (account_label) DO UPDATE SET
                    refresh_token = EXCLUDED.refresh_token,
                    external_account_id = EXCLUDED.external_account_id,
                    status = 'active',
                    client_type = 'web',
                    connected_at = now(),
                    connected_by = EXCLUDED.connected_by
                """,
                (account_label, display_name, refresh_token, external_account_id, connected_by),
            )
        conn.commit()
    finally:
        conn.close()


@router.get("/oauth/zoho/connect")
async def zoho_connect(token: str):
    row = _load_connect_token(token)
    if row is None:
        raise HTTPException(status_code=404, detail="Invalid or unknown invite link")
    if row["used_at"] is not None:
        raise HTTPException(status_code=410, detail="This invite link has already been used")
    if row["expires_at"] < now_utc():
        raise HTTPException(status_code=410, detail="This invite link has expired -- ask for a new one")

    params = {
        "scope": SCOPE,
        "client_id": os.environ["ZOHO_WEB_CLIENT_ID"],
        "response_type": "code",
        "access_type": "offline",
        "redirect_uri": _redirect_uri(),
        "prompt": "consent",
        "state": token,
    }
    return RedirectResponse(f"{AUTH_URL}?{urlencode(params)}")


@router.get("/oauth/zoho/callback")
async def zoho_callback(request: Request, code: str | None = None, state: str | None = None, error: str | None = None):
    if error:
        return HTMLResponse(f"<p>Zoho reported an error: {error}. Nothing was connected -- ask for a new link.</p>", status_code=400)
    if not code or not state:
        raise HTTPException(status_code=400, detail="Missing code/state")

    row = _load_connect_token(state)
    if row is None or row["used_at"] is not None or row["expires_at"] < now_utc():
        raise HTTPException(status_code=410, detail="This invite link is no longer valid -- ask for a new one")

    with httpx.Client(timeout=30.0) as client:
        token_resp = client.post(TOKEN_URL, data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": os.environ["ZOHO_WEB_CLIENT_ID"],
            "client_secret": os.environ["ZOHO_WEB_CLIENT_SECRET"],
            "redirect_uri": _redirect_uri(),
        })
        token_data = token_resp.json()
        refresh_token = token_data.get("refresh_token")
        access_token = token_data.get("access_token")
        if not refresh_token or not access_token:
            return HTMLResponse(
                f"<p>Zoho didn't return the expected tokens ({token_data.get('error', 'unknown error')}). "
                "Try the link again.</p>", status_code=400,
            )

        # Auto-discover the Zoho account_id -- see module docstring. Only
        # asking Isha to click Allow, never to look up or paste in an ID.
        accounts_resp = client.get(ACCOUNTS_API, headers={"Authorization": f"Zoho-oauthtoken {access_token}"})
        accounts_data = accounts_resp.json().get("data", [])
        external_account_id = accounts_data[0]["accountId"] if accounts_data else None
        if external_account_id is None:
            return HTMLResponse(
                "<p>Connected, but couldn't find a Zoho Mail account_id on this login -- "
                "contact Raj, something unexpected happened.</p>", status_code=500,
            )

    _upsert_account(row["account_label"], row["display_name"], refresh_token, external_account_id, connected_by=row["created_by"])
    _mark_token_used(state)

    return HTMLResponse(f"<p>{row['display_name']}'s Zoho Mail is connected. You can close this tab.</p>")


def seed_existing_account():
    """One-time backfill: moves support_zoho's refresh_token + hardcoded
    ZOHO_ACCOUNT_ID out of .env/module-constant and into oauth_accounts,
    so zoho_fetcher.py's switch to DB-backed account loading doesn't
    disrupt the account already in production. Safe to re-run."""
    refresh_token = os.environ.get("ZOHO_REFRESH_TOKEN")
    # Same value zoho_fetcher.py hardcoded pre-refactor -- not a secret,
    # Zoho's own numeric mail-account identifier.
    external_account_id = "5146160000000008002"
    if not refresh_token:
        print("skip support_zoho: no ZOHO_REFRESH_TOKEN in .env")
        return
    conn = get_live_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO oauth_accounts (account_label, display_name, source_type, refresh_token, external_account_id, client_type, connected_by)
                VALUES ('support_zoho', 'Support (shared)', 'zoho', %s, %s, 'legacy', 'seed-existing')
                ON CONFLICT (account_label) DO UPDATE SET
                    refresh_token = EXCLUDED.refresh_token,
                    external_account_id = EXCLUDED.external_account_id
                """,
                (refresh_token, external_account_id),
            )
        conn.commit()
    finally:
        conn.close()
    print("seeded support_zoho (client_type=legacy)")


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)

    invite = sub.add_parser("invite", help="Generate a one-time connect link for a new account")
    invite.add_argument("account_label", help="e.g. isha_zoho")
    invite.add_argument("display_name", help="e.g. Isha")
    invite.add_argument("--hours", type=int, default=48)
    invite.add_argument("--by", default=None, help="who generated this link, for the audit trail")

    sub.add_parser("seed-existing", help="Backfill support_zoho from .env into oauth_accounts")

    args = parser.parse_args()

    if args.cmd == "invite":
        url = generate_connect_link(args.account_label, args.display_name, created_by=args.by, ttl_hours=args.hours)
        print(url)
    elif args.cmd == "seed-existing":
        seed_existing_account()


if __name__ == "__main__":
    main()
