"""Gmail fetcher -- ports the Gmail-specific logic from the Render pipeline's
tools/email_to_obsidian/email_to_obsidian.py: refresh-token auth, thread
listing/pagination, num_retries=8 backoff, spam classification, dedup.
Final write step is Postgres (ingestion.write_email) instead of a markdown
file + git commit.

Accounts are DB-backed (oauth_accounts table, see schema/026_oauth_accounts.sql
and ingestion/oauth_gmail.py) rather than one hardcoded {PREFIX}_CLIENT_ID/
SECRET/REFRESH_TOKEN triplet per account in .env -- a newly-connected account
(via the self-serve OAuth flow) is picked up here with no code or .env change.
client_id/client_secret remain shared across every account (GMAIL_OAUTH_CLIENT_ID/
SECRET in .env, one OAuth app), only refresh_token is per-account and DB-stored.

Usage: python -m ingestion.gmail_fetcher --account raj_gmail [--dry-run] [--limit N]
"""
import argparse
import base64
import html
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from google.auth.exceptions import TransportError
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from ingestion.db import dual_write, get_live_conn
from ingestion.state import sync_since, set_last_synced_at, now_utc, is_message_seen, mark_messages_seen
from ingestion.spam_filter import is_eoxs_relevant
from ingestion.inline_tier_classifier import classify_tier
from ingestion.write_email import write_thread, existing_message_count
from ingestion.routing import load_client_index, classify_client
from ingestion.retry import call_with_retry
from ingestion.attachment_extract import extractable, extract_text

MAX_ATTACHMENT_DOWNLOAD_BYTES = 50 * 1024 * 1024  # matches the old vault pipeline's cutoff -- see extract_attachments

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("ingestion.gmail")

def load_accounts(raw_sweep_only=False):
    """Returns {account_label: display_name} for active oauth_accounts rows
    (source_type='gmail'). raw_sweep_only=True additionally filters to
    raw_sweep_enabled=true -- what run_gmail_all() should iterate; the
    unfiltered form is for one-off manual runs (python -m
    ingestion.gmail_fetcher --account remya_gmail still works even though
    remya is excluded from the recurring sweep)."""
    conn = get_live_conn()
    try:
        with conn.cursor() as cur:
            sql = "SELECT account_label, display_name FROM oauth_accounts WHERE source_type = 'gmail' AND status = 'active'"
            if raw_sweep_only:
                sql += " AND raw_sweep_enabled = true"
            cur.execute(sql)
            return {row["account_label"]: row["display_name"] for row in cur.fetchall()}
    finally:
        conn.close()


def _load_account_credentials(account):
    """Returns (refresh_token, client_id, client_secret). client_type picks
    which OAuth client's id/secret to refresh with -- a refresh_token is
    bound to whichever client originally minted it (see
    schema/027_oauth_client_type.sql): 'legacy_desktop' for raj/ron/remya
    (the original Desktop-type client, refresh_token predates this DB
    table), 'web' for every account connected via oauth_gmail.py's
    self-serve flow (a Google "Web application" client -- required for a
    custom HTTPS redirect_uri, which Desktop clients cannot register)."""
    conn = get_live_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT refresh_token, client_type FROM oauth_accounts WHERE account_label = %s AND source_type = 'gmail' AND status = 'active'",
                (account,),
            )
            row = cur.fetchone()
    finally:
        conn.close()
    if row is None:
        raise ValueError(f"no active oauth_accounts row for {account!r} -- connect it first (ingestion/oauth_gmail.py)")
    if row["client_type"] == "legacy_desktop":
        client_id = os.environ["GMAIL_OAUTH_CLIENT_ID"]
        client_secret = os.environ["GMAIL_OAUTH_CLIENT_SECRET"]
    else:
        client_id = os.environ["GMAIL_OAUTH_WEB_CLIENT_ID"]
        client_secret = os.environ["GMAIL_OAUTH_WEB_CLIENT_SECRET"]
    return row["refresh_token"], client_id, client_secret


GMAIL_NUM_RETRIES = 8
DEFAULT_MAX_RESULTS = 500
DEFAULT_SAFETY_OVERLAP_DAYS = 2


def _is_retryable(e):
    """429/5xx and transport-level failures are transient; anything else
    (400/401/403/404/...) means retrying won't help. googleapiclient's own
    num_retries already backs off within a single .execute() call -- this
    is a belt-and-suspenders outer layer for failures it doesn't cover
    (credential refresh, errors raised before any HTTP response)."""
    if isinstance(e, HttpError):
        status = e.resp.status
        return status == 429 or status >= 500
    return isinstance(e, (TransportError, ConnectionError, TimeoutError))


def _retry_after(e):
    if isinstance(e, HttpError):
        value = e.resp.get("retry-after")
        if value is not None:
            try:
                return float(value)
            except ValueError:
                return None
    return None


def get_gmail_service(account):
    refresh_token, client_id, client_secret = _load_account_credentials(account)
    creds = Credentials(
        None,
        refresh_token=refresh_token,
        client_id=client_id,
        client_secret=client_secret,
        token_uri="https://oauth2.googleapis.com/token",
        scopes=["https://www.googleapis.com/auth/gmail.readonly"],
    )
    call_with_retry(lambda: creds.refresh(Request()), is_retryable=_is_retryable, retry_after_getter=_retry_after)
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def gmail_after_query(since):
    if since is None:
        return None
    return f"after:{since.strftime('%Y/%m/%d')}"


def fetch_thread_ids(service, query, max_results):
    ids = []
    page_token = None
    while len(ids) < max_results:
        request = service.users().threads().list(
            userId="me", q=query, maxResults=min(100, max_results - len(ids)),
            pageToken=page_token,
        )
        resp = call_with_retry(
            lambda: request.execute(num_retries=GMAIL_NUM_RETRIES),
            is_retryable=_is_retryable, retry_after_getter=_retry_after,
        )
        ids.extend(t["id"] for t in resp.get("threads", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return ids


def decode_body(payload):
    """Extracts a plain-text body from a Gmail message payload, walking
    multipart structures, preferring text/plain."""
    if payload.get("mimeType") == "text/plain" and payload.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="replace")
    for part in payload.get("parts", []) or []:
        text = decode_body(part)
        if text:
            return text
    if payload.get("body", {}).get("data"):
        raw = base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="replace")
        return re.sub(r"<[^>]+>", " ", html.unescape(raw))  # crude HTML strip fallback
    return ""


def header(headers, name):
    for h in headers:
        if h["name"].lower() == name.lower():
            return h["value"]
    return None


def _content_disposition_is_inline(part):
    for h in part.get("headers", []) or []:
        if h.get("name", "").lower() == "content-disposition":
            return h.get("value", "").lower().strip().startswith("inline")
    return any(h.get("name", "").lower() == "content-id" for h in part.get("headers", []) or [])


def find_gmail_attachment_parts(payload):
    """Recursively walks payload.parts collecting every part that carries a
    non-empty filename -- Gmail's signal for "this part is an attachment,
    not inline body text". Free/zero-API-cost: already in the fetched
    thread detail (format="full"), no extra request needed."""
    found = []
    if payload.get("filename"):
        found.append(payload)
    for part in payload.get("parts", []) or []:
        found.extend(find_gmail_attachment_parts(part))
    return found


def _download_gmail_attachment(service, message_id, attachment_id):
    """Returns raw bytes, or None on failure -- never raises (mirrors the
    old vault pipeline's download_gmail_attachments error handling)."""
    try:
        resp = call_with_retry(
            lambda: service.users().messages().attachments().get(
                userId="me", messageId=message_id, id=attachment_id
            ).execute(num_retries=GMAIL_NUM_RETRIES),
            is_retryable=_is_retryable, retry_after_getter=_retry_after,
        )
    except Exception as e:
        logger.warning("attachment download failed (message=%s, attachment=%s): %s", message_id, attachment_id, e)
        return None
    data = resp.get("data", "")
    if not data:
        return None
    return base64.urlsafe_b64decode(data + "==")


def extract_attachments(payload, service=None, message_id=None):
    """Always records metadata (filename/size/note) for every attachment
    part found -- nothing is silently dropped, inline images (signature
    logos, tracking pixels) are still recorded, just noted as such.

    2026-08: also captures the native Gmail attachmentId (previously
    fetched from the API response and then discarded -- without it, an
    already-ingested attachment could never be re-fetched later, only
    recovered by a full re-scan) and, for extractable document formats
    (pdf/docx/xlsx/csv -- see ingestion/attachment_extract.py) under
    MAX_ATTACHMENT_DOWNLOAD_BYTES, downloads the real content and stores
    extracted text. Images and other non-document formats are still
    fully recorded (filename/size), just never downloaded -- no OCR, no
    reason to spend an API call and disk-adjacent DB storage on bytes
    nothing can read yet."""
    attachments = []
    for part in find_gmail_attachment_parts(payload):
        size = part.get("body", {}).get("size")
        try:
            size_int = int(size)
        except (TypeError, ValueError):
            size_int = None
        attachment_id = part.get("body", {}).get("attachmentId", "")
        filename = part.get("filename") or f"attachment-{attachment_id}"
        note = "inline image" if _content_disposition_is_inline(part) else None
        mimetype = part.get("mimeType")
        extracted_text = None
        if (
            service is not None and message_id and attachment_id
            and extractable(filename)
            and (size_int is None or size_int < MAX_ATTACHMENT_DOWNLOAD_BYTES)
        ):
            data = _download_gmail_attachment(service, message_id, attachment_id)
            if data is not None:
                extracted_text = extract_text(filename, data)
        attachments.append({
            "filename": filename,
            "size_bytes": size_int,
            "note": note,
            "source_attachment_id": attachment_id or None,
            "mimetype": mimetype,
            "extracted_text": extracted_text,
        })
    return attachments


def fetch_thread_detail(service, thread_id):
    request = service.users().threads().get(userId="me", id=thread_id, format="full")
    thread = call_with_retry(
        lambda: request.execute(num_retries=GMAIL_NUM_RETRIES),
        is_retryable=_is_retryable, retry_after_getter=_retry_after,
    )
    messages = thread.get("messages", [])
    if not messages:
        return None

    first_headers = messages[0]["payload"]["headers"]
    subject = header(first_headers, "Subject") or "(no subject)"
    from_addr = header(first_headers, "From")
    to_addr = header(first_headers, "To")

    participants = set()
    msg_records = []
    message_ids = []
    attachments_by_message_index = {}
    for i, m in enumerate(messages):
        headers_i = m["payload"]["headers"]
        from_i = header(headers_i, "From") or ""
        m_email = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", from_i)
        if m_email:
            participants.add(m_email.group(0).lower())
        internal_date = m.get("internalDate")
        msg_date = (
            datetime.fromtimestamp(int(internal_date) / 1000, tz=timezone.utc)
            if internal_date else None
        )
        msg_records.append({
            "message_index": i + 1,
            "message_date": msg_date,
            "from_addr": from_i,
            "body": decode_body(m["payload"]).strip(),
        })
        message_ids.append(m["id"])

        atts = extract_attachments(m["payload"], service=service, message_id=m["id"])
        if atts:
            attachments_by_message_index[i + 1] = atts

    return {
        "gmail_thread_id": thread_id,
        "subject": subject,
        "from_addr": from_addr,
        "to_addr": to_addr,
        "message_count": len(messages),
        "participants": sorted(participants),
        "thread_dates": [m["message_date"] for m in msg_records if m["message_date"]],
        "messages": msg_records,
        "message_ids": message_ids,
        "attachments_by_message_index": attachments_by_message_index,
    }


def process_account(account, *, dry_run=False, limit=DEFAULT_MAX_RESULTS,
                     safety_overlap_days=DEFAULT_SAFETY_OVERLAP_DAYS, classify=True):
    service = get_gmail_service(account)
    since = sync_since(account, safety_overlap_days)
    query = gmail_after_query(since)
    logger.info("account=%s query=%r limit=%d dry_run=%s", account, query, limit, dry_run)

    thread_ids = fetch_thread_ids(service, query, limit)
    logger.info("account=%s candidate threads=%d", account, len(thread_ids))

    counts = {"written": 0, "skipped_stale": 0, "skipped_spam": 0, "skipped_seen": 0, "error": 0, "written_items": []}
    run_started_at = now_utc()
    client_index = load_client_index()

    for tid in thread_ids:
        try:
            detail = fetch_thread_detail(service, tid)
            if not detail:
                continue

            conn = get_live_conn()
            try:
                existing_count = existing_message_count(conn, account, tid)
            finally:
                conn.close()

            if existing_count is not None and existing_count >= detail["message_count"]:
                counts["skipped_stale"] += 1
                continue

            if all(is_message_seen(mid) for mid in detail["message_ids"]) and detail["message_ids"]:
                counts["skipped_seen"] += 1
                continue

            if classify and not is_eoxs_relevant(detail["subject"], detail["messages"][0]["body"] if detail["messages"] else ""):
                counts["skipped_spam"] += 1
                mark_messages_seen(detail["message_ids"], account)
                continue

            if dry_run:
                logger.info("[dry-run] would write thread %s: %r (%d messages)",
                            tid, detail["subject"], detail["message_count"])
                counts["written"] += 1
                counts["written_items"].append(detail["subject"])
                continue

            first_body = detail["messages"][0]["body"] if detail["messages"] else ""
            tier_context = f"Email subject: {detail['subject']}\nBody (truncated):\n{first_body}"
            dual_write(
                write_thread,
                source_account=account, gmail_thread_id=detail["gmail_thread_id"],
                subject=detail["subject"], from_addr=detail["from_addr"], to_addr=detail["to_addr"],
                message_count=detail["message_count"], participants=detail["participants"],
                thread_dates=detail["thread_dates"], tags=["email", account],
                is_quarantined=False, generated_at=now_utc(), messages=detail["messages"],
                attachments_by_message_index=detail["attachments_by_message_index"],
                client_id=classify_client(client_index, detail["participants"]),
                access_tier=classify_tier(tier_context),
            )
            mark_messages_seen(detail["message_ids"], account)
            counts["written"] += 1
            counts["written_items"].append(detail["subject"])

        except Exception as e:
            logger.error("thread %s failed: %s", tid, e)
            counts["error"] += 1

    if not dry_run:
        set_last_synced_at(account, run_started_at)

    logger.info("account=%s done: %s", account, counts)
    return counts


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--account", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=DEFAULT_MAX_RESULTS)
    parser.add_argument("--no-classify", action="store_true")
    args = parser.parse_args()

    if args.account not in load_accounts():
        parser.error(f"{args.account!r} is not a connected, active gmail account in oauth_accounts")

    process_account(
        args.account, dry_run=args.dry_run, limit=args.limit,
        classify=not args.no_classify,
    )


if __name__ == "__main__":
    main()
