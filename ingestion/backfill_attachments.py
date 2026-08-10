"""Backfills email_attachments.source_attachment_id/mimetype/extracted_text
for rows written before migration 024 added those columns -- original
Gmail/Zoho ingestion only ever captured filename/size metadata (see
schema/015's comment), so the provider's own attachment id was never
stored and has to be re-derived by re-fetching each thread.

Operates per-thread (not per-attachment) to keep API calls cheap: a single
Gmail threads.get(format="full") or Zoho message-content fetch returns
every part's mimeType/attachmentId for free, so bytes are only downloaded
for attachments whose extension is in EXTRACTORS and whose size is under
MAX_EXTRACT_BYTES. Non-extractable types (images, .ics, archives, ...)
still get source_attachment_id/mimetype filled from the free metadata.

DB rows are matched to freshly-fetched provider parts within the same
message: first by exact normalized filename, then (for whatever's left)
by a hash-suffix-stripped fallback -- see normalize_filename/
dehashed_filename for why both passes are needed. A DB row with no
matching provider part (message or attachment deleted upstream since
original ingestion) is counted "gone" and left untouched, never guessed.

Usage:
    python -m ingestion.backfill_attachments --account raj_gmail --shard-index 0 --shard-count 6
    python -m ingestion.backfill_attachments --zoho
"""
import argparse
import base64
import logging
import mimetypes
import os
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from google.auth.exceptions import TransportError
from googleapiclient.errors import HttpError

from ingestion.attachment_extract import MAX_EXTRACT_BYTES, extract_text
from ingestion.db import get_live_conn
from ingestion.gmail_fetcher import ACCOUNTS, GMAIL_NUM_RETRIES, find_gmail_attachment_parts, get_gmail_service
from ingestion.retry import call_with_retry
from ingestion.zoho_fetcher import ZohoClient, build_thread_groups

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("ingestion.backfill_attachments")

PROGRESS_EVERY = 25
MARKDOWN_LINK_RE = re.compile(r"^\[(?P<name>.+)\]\(.*\)$")


def clean_filename(raw):
    """Old file-based-loader rows stored the raw markdown bullet
    "[name](attachments/.../name)" verbatim as filename -- strip it back
    to a bare name for matching against a provider's attachmentName."""
    m = MARKDOWN_LINK_RE.match((raw or "").strip())
    return m.group("name") if m else raw


DROP_CHARS_RE = re.compile(r"[^\w\s-]")
SEPARATOR_RE = re.compile(r"[_\s]+")
MULTI_HYPHEN_RE = re.compile(r"-+")
HASH_SUFFIX_RE = re.compile(r"^(?P<base>.+)-(?P<hash>[0-9a-f]{8,16})$")


def normalize_filename(name):
    """The old file-based loader slugified filenames for filesystem safety
    before storing them -- e.g. live Zoho
    "Screenshot_23-7-2026_81847_discountpipesteel.eoxs.com.jpeg" was stored
    as "screenshot-23-7-2026-81847-discountpipesteeleoxscom.jpeg": dots/
    parens/etc. are dropped outright (no separator), while underscores and
    whitespace become a single '-' (verified against several real rows,
    both punctuation-drop and underscore-to-hyphen behave differently, so
    a single "replace all non-alnum with '-'" rule under- or
    over-separates depending on which case you check first). Matching on
    raw filename against a freshly re-fetched provider part therefore
    misses every old-loader row; both sides are normalized through this
    same slug before comparing so DB-native rows (already close to their
    live name) and old-loader rows (already slugified) land on the same
    key."""
    stem, ext = os.path.splitext(name or "")
    stem = DROP_CHARS_RE.sub("", stem)
    stem = SEPARATOR_RE.sub("-", stem)
    stem = MULTI_HYPHEN_RE.sub("-", stem).strip("-").lower()
    return f"{stem}{ext.lower()}"


def dehashed_filename(normalized_name):
    """The old loader also appended a short hex hash to disambiguate two
    attachments in the same thread that slugify to the same name (e.g. the
    same file re-attached in a later message) -- e.g.
    "non-hdfc-disbursement-letters-1-0f60825c8f.xlsx". Live provider names
    never carry this, so it's stripped for a fallback comparison after an
    exact match fails."""
    stem, ext = os.path.splitext(normalized_name)
    m = HASH_SUFFIX_RE.match(stem)
    return f"{m.group('base')}{ext}" if m else normalized_name


def match_attachments(db_rows, api_parts):
    """db_rows/api_parts: lists of (obj, raw_filename). Returns [(db_obj,
    api_obj|None)] -- each db row matched at most once, preferring an exact
    normalized-name match and falling back to a hash-stripped match (see
    dehashed_filename) for whatever's left, both within this single
    message's attachment list so unrelated messages never cross-match."""
    api_entries = [
        {"obj": obj, "exact": normalize_filename(name), "used": False}
        for obj, name in api_parts
    ]
    for e in api_entries:
        e["dehash"] = dehashed_filename(e["exact"])

    pairs = []
    unmatched = []
    for obj, name in db_rows:
        exact = normalize_filename(name)
        match = next((e for e in api_entries if not e["used"] and e["exact"] == exact), None)
        if match:
            match["used"] = True
            pairs.append((obj, match["obj"]))
        else:
            unmatched.append((obj, dehashed_filename(exact)))

    for obj, dehash in unmatched:
        match = next((e for e in api_entries if not e["used"] and e["dehash"] == dehash), None)
        if match:
            match["used"] = True
            pairs.append((obj, match["obj"]))
        else:
            pairs.append((obj, None))
    return pairs


# --- shared DB helpers -------------------------------------------------------

def update_attachment(conn, attachment_id, mimetype, source_attachment_id, extracted_text_value):
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE email_attachments
            SET mimetype = COALESCE(%s, mimetype),
                source_attachment_id = COALESCE(%s, source_attachment_id),
                extracted_text = COALESCE(%s, extracted_text)
            WHERE id = %s
            """,
            (mimetype, source_attachment_id, extracted_text_value, attachment_id),
        )
    conn.commit()


def threads_needing_backfill(source_account):
    conn = get_live_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT et.id, et.gmail_thread_id
                FROM email_threads et
                JOIN email_attachments ea ON ea.thread_id = et.id
                WHERE et.source_account = %s
                  AND (ea.source_attachment_id IS NULL OR ea.mimetype IS NULL)
                ORDER BY et.id
                """,
                (source_account,),
            )
            return cur.fetchall()
    finally:
        conn.close()


def rows_needing_backfill(conn, thread_db_id):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ea.id, ea.filename, ea.size_bytes, em.message_index
            FROM email_attachments ea
            JOIN email_messages em ON em.id = ea.message_id
            WHERE ea.thread_id = %s AND (ea.source_attachment_id IS NULL OR ea.mimetype IS NULL)
            ORDER BY ea.id
            """,
            (thread_db_id,),
        )
        return cur.fetchall()


def log_progress(source, i, total, counts, started):
    if i % PROGRESS_EVERY != 0 and i != total:
        return
    elapsed = time.monotonic() - started
    rate = i / elapsed if elapsed else 0
    eta_min = ((total - i) / rate / 60) if rate > 0 else 0.0
    logger.info(
        "%s progress %d/%d written=%d gone=%d error=%d elapsed=%ds eta=%.1fmin",
        source, i, total, counts["written"], counts["gone"], counts["error"], int(elapsed), eta_min,
    )


# --- Gmail -------------------------------------------------------------------

def _gmail_is_retryable(e):
    if isinstance(e, HttpError):
        return e.resp.status == 429 or e.resp.status >= 500
    return isinstance(e, (TransportError, ConnectionError, TimeoutError))


def _gmail_retry_after(e):
    if isinstance(e, HttpError):
        value = e.resp.get("retry-after")
        if value is not None:
            try:
                return float(value)
            except ValueError:
                return None
    return None


def _gmail_execute(request):
    return call_with_retry(
        lambda: request.execute(num_retries=GMAIL_NUM_RETRIES),
        is_retryable=_gmail_is_retryable, retry_after_getter=_gmail_retry_after,
    )


def fetch_thread_parts_by_message(service, gmail_thread_id):
    """{message_index: [(part_obj, raw_filename), ...]}"""
    thread = _gmail_execute(service.users().threads().get(userId="me", id=gmail_thread_id, format="full"))
    parts_by_index = defaultdict(list)
    for i, m in enumerate(thread.get("messages", [])):
        message_index = i + 1
        for part in find_gmail_attachment_parts(m["payload"]):
            filename = part.get("filename") or f"attachment-{part.get('body', {}).get('attachmentId', '')}"
            parts_by_index[message_index].append(({
                "gmail_message_id": m["id"],
                "attachment_id": part.get("body", {}).get("attachmentId"),
                "mimetype": part.get("mimeType") or "application/octet-stream",
                "size": part.get("body", {}).get("size") or 0,
            }, filename))
    return parts_by_index


def download_gmail_attachment(service, message_id, attachment_id):
    request = service.users().messages().attachments().get(userId="me", messageId=message_id, id=attachment_id)
    resp = _gmail_execute(request)
    return base64.urlsafe_b64decode(resp["data"])


def process_gmail_attachment(conn, service, db_row, api_part, counts):
    extracted_text_value = None
    if api_part["attachment_id"] and api_part["size"] <= MAX_EXTRACT_BYTES:
        try:
            data = download_gmail_attachment(service, api_part["gmail_message_id"], api_part["attachment_id"])
            extracted_text_value = extract_text(data, db_row["filename"])
        except Exception as e:
            logger.warning("extract failed id=%s filename=%r: %s", db_row["id"], db_row["filename"], e)
    try:
        update_attachment(conn, db_row["id"], api_part["mimetype"], api_part["attachment_id"], extracted_text_value)
        counts["written"] += 1
    except Exception as e:
        logger.error("db update failed id=%s: %s", db_row["id"], e)
        counts["error"] += 1


def process_account(account, shard_index, shard_count):
    service = get_gmail_service(account)
    all_threads = threads_needing_backfill(account)
    shard = [t for t in all_threads if t["id"] % shard_count == shard_index]
    total = len(shard)
    logger.info("account=%s shard=%d/%d threads to backfill=%d", account, shard_index, shard_count, total)

    counts = {"written": 0, "gone": 0, "error": 0}
    started = time.monotonic()
    for i, thread in enumerate(shard, 1):
        conn = get_live_conn()
        try:
            db_rows = rows_needing_backfill(conn, thread["id"])
            db_by_index = defaultdict(list)
            for r in db_rows:
                db_by_index[r["message_index"]].append((r, clean_filename(r["filename"])))

            try:
                api_by_index = fetch_thread_parts_by_message(service, thread["gmail_thread_id"])
            except HttpError as e:
                if e.resp.status == 404:
                    counts["gone"] += len(db_rows)
                    continue
                raise

            for message_index, db_items in db_by_index.items():
                for db_row, api_part in match_attachments(db_items, api_by_index.get(message_index, [])):
                    if api_part is None:
                        counts["gone"] += 1
                        continue
                    process_gmail_attachment(conn, service, db_row, api_part, counts)
        except Exception as e:
            logger.error("thread %s failed: %s", thread["gmail_thread_id"], e)
            counts["error"] += 1
        finally:
            conn.close()

        log_progress(f"account={account}", i, total, counts, started)

    logger.info("account=%s done: %s", account, counts)
    return counts


# --- Zoho ----------------------------------------------------------------

def process_zoho_attachment(conn, client, db_row, api_att, counts):
    clean_name = clean_filename(db_row["filename"])
    mimetype = api_att.get("content_type") or mimetypes.guess_type(clean_name)[0] or "application/octet-stream"
    attachment_id = api_att.get("attachment_id")
    extracted_text_value = None
    if attachment_id and api_att["size"] <= MAX_EXTRACT_BYTES:
        try:
            data = client.fetch_attachment_content(api_att["folder_id"], api_att["message_id"], attachment_id)
            extracted_text_value = extract_text(data, clean_name)
        except Exception as e:
            logger.warning("zoho extract failed id=%s filename=%r: %s", db_row["id"], clean_name, e)
    try:
        update_attachment(conn, db_row["id"], mimetype, attachment_id, extracted_text_value)
        counts["written"] += 1
    except Exception as e:
        logger.error("zoho db update failed id=%s: %s", db_row["id"], e)
        counts["error"] += 1


def process_zoho():
    client = ZohoClient()
    threads = threads_needing_backfill("support_zoho")
    logger.info("zoho threads to backfill=%d", len(threads))

    counts = {"written": 0, "gone": 0, "error": 0}
    if not threads:
        logger.info("zoho backfill done: %s", counts)
        return counts

    messages = client.list_all_messages(after_epoch_ms=0, max_results=20000)
    thread_groups = build_thread_groups(messages)
    logger.info("zoho: fetched %d historical messages, %d distinct threads total", len(messages), len(thread_groups))

    for thread in threads:
        zoho_thread_id = thread["gmail_thread_id"].removeprefix("zoho-")
        conn = get_live_conn()
        try:
            db_rows = rows_needing_backfill(conn, thread["id"])
            msgs = thread_groups.get(zoho_thread_id)
            if not msgs:
                logger.warning("zoho thread=%s not found in historical message list, skipping", thread["gmail_thread_id"])
                counts["gone"] += len(db_rows)
                continue

            api_items = []
            for m in sorted(msgs, key=lambda m: int(m.get("receivedTime", 0))):
                if str(m.get("hasAttachment")) != "1":
                    continue
                for att in client.fetch_attachment_info(m["folderId"], m["messageId"]):
                    name = att.get("attachmentName") or f"attachment-{att.get('attachmentId', '')}"
                    try:
                        size = int(att.get("attachmentSize") or 0)
                    except (TypeError, ValueError):
                        size = 0
                    api_items.append(({
                        "folder_id": m["folderId"], "message_id": m["messageId"],
                        "attachment_id": att.get("attachmentId"),
                        "content_type": att.get("contentType"),
                        "size": size,
                    }, name))

            db_items = [(row, clean_filename(row["filename"])) for row in db_rows]
            for db_row, api_att in match_attachments(db_items, api_items):
                if api_att is None:
                    counts["gone"] += 1
                    continue
                process_zoho_attachment(conn, client, db_row, api_att, counts)
        except Exception as e:
            logger.error("zoho thread %s failed: %s", thread["gmail_thread_id"], e)
            counts["error"] += 1
        finally:
            conn.close()

    logger.info("zoho backfill done: %s", counts)
    return counts


def main():
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--account", choices=list(ACCOUNTS.keys()))
    group.add_argument("--zoho", action="store_true")
    parser.add_argument("--shard-index", type=int, default=0)
    parser.add_argument("--shard-count", type=int, default=1)
    args = parser.parse_args()

    if args.zoho:
        result = process_zoho()
    else:
        result = process_account(args.account, args.shard_index, args.shard_count)
    print(result)


if __name__ == "__main__":
    main()
