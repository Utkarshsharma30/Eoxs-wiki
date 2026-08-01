"""Zoho Mail fetcher -- ports the ZohoClient logic from the Render
pipeline's tools/email_to_obsidian/email_to_obsidian.py: OAuth2 refresh
(access token cached in-memory, auto-refresh + retry-once on 401), unified
/messages/search endpoint with searchKey="date:" (spans every folder
except Spam/Trash), client-side date filtering (Zoho's fromDate/toDate
search operators are unreliable per the original pipeline's notes), and
a two-step fetch (cheap list, then per-message content).

Usage: python -m ingestion.zoho_fetcher [--dry-run] [--limit N]
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

import httpx

from ingestion.db import dual_write
from ingestion.state import sync_since, set_last_synced_at, now_utc, is_message_seen, mark_messages_seen
from ingestion.spam_filter import is_eoxs_relevant
from ingestion.write_email import write_thread, existing_message_count
from ingestion.db import get_live_conn

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("ingestion.zoho")

SOURCE = "support_zoho"
ACCOUNTS_BASE = "https://accounts.zoho.com"
MAIL_API_BASE = "https://mail.zoho.com/api"
DEFAULT_MAX_RESULTS = 20000
DEFAULT_SAFETY_OVERLAP_DAYS = 2
PAGE_SIZE = 200


class ZohoClient:
    def __init__(self):
        self.account_id = os.environ.get("ZOHO_ACCOUNT_ID")  # optional; unified search doesn't need per-account scoping
        self._access_token = None
        self._client = httpx.Client(timeout=30.0)

    def _refresh_access_token(self):
        resp = self._client.post(
            f"{ACCOUNTS_BASE}/oauth/v2/token",
            data={
                "refresh_token": os.environ["ZOHO_REFRESH_TOKEN"],
                "client_id": os.environ["ZOHO_CLIENT_ID"],
                "client_secret": os.environ["ZOHO_CLIENT_SECRET"],
                "grant_type": "refresh_token",
            },
        )
        resp.raise_for_status()
        self._access_token = resp.json()["access_token"]

    def _request(self, method, url, retried=False, **kwargs):
        if self._access_token is None:
            self._refresh_access_token()
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Zoho-oauthtoken {self._access_token}"
        resp = self._client.request(method, url, headers=headers, **kwargs)
        if resp.status_code == 401 and not retried:
            self._refresh_access_token()
            return self._request(method, url, retried=True, headers=headers, **kwargs)
        resp.raise_for_status()
        return resp

    def list_all_messages(self, after_epoch_ms, max_results):
        messages = []
        seen_ids = set()
        start = 1
        while len(messages) < max_results:
            resp = self._request(
                "GET", f"{MAIL_API_BASE}/messages/search",
                params={"searchKey": "date:", "start": start, "limit": PAGE_SIZE},
            )
            data = resp.json().get("data", [])
            if not data:
                break
            for m in data:
                mid = m.get("messageId")
                if mid in seen_ids:
                    continue
                received = int(m.get("receivedTime", 0))
                if received and received < after_epoch_ms:
                    continue
                seen_ids.add(mid)
                messages.append(m)
            if len(data) < PAGE_SIZE:
                break
            start += PAGE_SIZE
            if len(messages) >= max_results:
                break
        return messages[:max_results]

    def fetch_message_content(self, folder_id, message_id):
        resp = self._request(
            "GET", f"{MAIL_API_BASE}/folders/{folder_id}/messages/{message_id}/content"
        )
        return resp.json().get("data", {})


def decode_zoho_body(content_data):
    raw = content_data.get("content", "")
    if not raw:
        return ""
    text = re.sub(r"<[^>]+>", " ", html.unescape(raw))
    return re.sub(r"\s+", " ", text).strip()


def build_thread_groups(messages):
    """Groups messages by threadId (fallback to messageId if absent)."""
    groups = {}
    for m in messages:
        tid = m.get("threadId") or m["messageId"]
        groups.setdefault(tid, []).append(m)
    return groups


def process_zoho(*, dry_run=False, limit=DEFAULT_MAX_RESULTS,
                  safety_overlap_days=DEFAULT_SAFETY_OVERLAP_DAYS, classify=True):
    client = ZohoClient()
    since = sync_since(SOURCE, safety_overlap_days)
    after_epoch_ms = int(since.timestamp() * 1000) if since else 0
    logger.info("source=%s since=%s limit=%d dry_run=%s", SOURCE, since, limit, dry_run)

    messages = client.list_all_messages(after_epoch_ms, limit)
    logger.info("source=%s candidate messages=%d", SOURCE, len(messages))

    thread_groups = build_thread_groups(messages)
    logger.info("source=%s candidate threads=%d", SOURCE, len(thread_groups))

    counts = {"written": 0, "skipped_stale": 0, "skipped_spam": 0, "skipped_seen": 0, "error": 0}
    run_started_at = now_utc()

    for thread_id, msgs in thread_groups.items():
        try:
            msgs.sort(key=lambda m: int(m.get("receivedTime", 0)))
            message_ids = [m["messageId"] for m in msgs]

            if all(is_message_seen(mid) for mid in message_ids):
                counts["skipped_seen"] += 1
                continue

            conn = get_live_conn()
            try:
                existing_count = existing_message_count(conn, SOURCE, thread_id)
            finally:
                conn.close()
            if existing_count is not None and existing_count >= len(msgs):
                counts["skipped_stale"] += 1
                continue

            msg_records = []
            for i, m in enumerate(msgs):
                content = client.fetch_message_content(m["folderId"], m["messageId"])
                body = decode_zoho_body(content)
                received = int(m.get("receivedTime", 0))
                msg_date = datetime.fromtimestamp(received / 1000, tz=timezone.utc) if received else None
                msg_records.append({
                    "message_index": i + 1,
                    "message_date": msg_date,
                    "from_addr": m.get("fromAddress"),
                    "body": body,
                })

            subject = msgs[0].get("subject", "(no subject)")
            participants = sorted({
                m.get("fromAddress", "").lower() for m in msgs if m.get("fromAddress")
            })

            if classify:
                first_body = msg_records[0]["body"] if msg_records else ""
                if not is_eoxs_relevant(subject, first_body):
                    counts["skipped_spam"] += 1
                    mark_messages_seen(message_ids, SOURCE)
                    continue

            if dry_run:
                logger.info("[dry-run] would write thread %s: %r (%d messages)",
                            thread_id, subject, len(msgs))
                counts["written"] += 1
                continue

            dual_write(
                write_thread,
                source_account=SOURCE, gmail_thread_id=thread_id,
                subject=subject, from_addr=msgs[0].get("fromAddress"),
                to_addr=msgs[0].get("toAddress"),
                message_count=len(msgs), participants=participants,
                thread_dates=[m["message_date"] for m in msg_records if m["message_date"]],
                tags=["email", SOURCE], is_quarantined=False, generated_at=now_utc(),
                messages=msg_records,
            )
            mark_messages_seen(message_ids, SOURCE)
            counts["written"] += 1

        except Exception as e:
            logger.error("thread %s failed: %s", thread_id, e)
            counts["error"] += 1

    if not dry_run:
        set_last_synced_at(SOURCE, run_started_at)

    logger.info("source=%s done: %s", SOURCE, counts)
    return counts


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=DEFAULT_MAX_RESULTS)
    parser.add_argument("--no-classify", action="store_true")
    args = parser.parse_args()

    process_zoho(dry_run=args.dry_run, limit=args.limit, classify=not args.no_classify)


if __name__ == "__main__":
    main()
