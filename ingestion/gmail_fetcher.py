"""Gmail fetcher -- ports the Gmail-specific logic from the Render pipeline's
tools/email_to_obsidian/email_to_obsidian.py: refresh-token auth, thread
listing/pagination, num_retries=8 backoff, spam classification, dedup.
Final write step is Postgres (ingestion.write_email) instead of a markdown
file + git commit.

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

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

from ingestion.db import dual_write, get_live_conn
from ingestion.state import sync_since, set_last_synced_at, now_utc, is_message_seen, mark_messages_seen
from ingestion.spam_filter import is_eoxs_relevant
from ingestion.write_email import write_thread, existing_message_count

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("ingestion.gmail")

ACCOUNTS = {
    "raj_gmail": "RAJ_GMAIL",
    "ron_gmail": "RON_GMAIL",
    "remya_gmail": "REMYA_GMAIL",
}

GMAIL_NUM_RETRIES = 8
DEFAULT_MAX_RESULTS = 500
DEFAULT_SAFETY_OVERLAP_DAYS = 2


def get_gmail_service(account):
    prefix = ACCOUNTS[account]
    creds = Credentials(
        None,
        refresh_token=os.environ[f"{prefix}_REFRESH_TOKEN"],
        client_id=os.environ[f"{prefix}_CLIENT_ID"],
        client_secret=os.environ[f"{prefix}_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token",
        scopes=["https://www.googleapis.com/auth/gmail.readonly"],
    )
    creds.refresh(Request())
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def gmail_after_query(since):
    if since is None:
        return None
    return f"after:{since.strftime('%Y/%m/%d')}"


def fetch_thread_ids(service, query, max_results):
    ids = []
    page_token = None
    while len(ids) < max_results:
        resp = service.users().threads().list(
            userId="me", q=query, maxResults=min(100, max_results - len(ids)),
            pageToken=page_token,
        ).execute(num_retries=GMAIL_NUM_RETRIES)
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


def fetch_thread_detail(service, thread_id):
    thread = service.users().threads().get(userId="me", id=thread_id, format="full").execute(
        num_retries=GMAIL_NUM_RETRIES
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
    }


def process_account(account, *, dry_run=False, limit=DEFAULT_MAX_RESULTS,
                     safety_overlap_days=DEFAULT_SAFETY_OVERLAP_DAYS, classify=True):
    service = get_gmail_service(account)
    since = sync_since(account, safety_overlap_days)
    query = gmail_after_query(since)
    logger.info("account=%s query=%r limit=%d dry_run=%s", account, query, limit, dry_run)

    thread_ids = fetch_thread_ids(service, query, limit)
    logger.info("account=%s candidate threads=%d", account, len(thread_ids))

    counts = {"written": 0, "skipped_stale": 0, "skipped_spam": 0, "skipped_seen": 0, "error": 0}
    run_started_at = now_utc()

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
                continue

            dual_write(
                write_thread,
                source_account=account, gmail_thread_id=detail["gmail_thread_id"],
                subject=detail["subject"], from_addr=detail["from_addr"], to_addr=detail["to_addr"],
                message_count=detail["message_count"], participants=detail["participants"],
                thread_dates=detail["thread_dates"], tags=["email", account],
                is_quarantined=False, generated_at=now_utc(), messages=detail["messages"],
            )
            mark_messages_seen(detail["message_ids"], account)
            counts["written"] += 1

        except Exception as e:
            logger.error("thread %s failed: %s", tid, e)
            counts["error"] += 1

    if not dry_run:
        set_last_synced_at(account, run_started_at)

    logger.info("account=%s done: %s", account, counts)
    return counts


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--account", required=True, choices=list(ACCOUNTS.keys()))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=DEFAULT_MAX_RESULTS)
    parser.add_argument("--no-classify", action="store_true")
    args = parser.parse_args()

    process_account(
        args.account, dry_run=args.dry_run, limit=args.limit,
        classify=not args.no_classify,
    )


if __name__ == "__main__":
    main()
