"""One-time historical backfill for the attachment-extraction feature
(schema/024 + the download/extract wiring added to gmail_fetcher.py/
zoho_fetcher.py). Every thread ingested before that change has
attachments with source_attachment_id/mimetype/extracted_text all NULL
-- this re-fetches those threads from the live Gmail/Zoho APIs and
rewrites them with write_thread(), which now captures all three.

Deliberately bypasses process_account's/process_zoho's normal
already-seen/stale skip logic -- that logic exists to avoid reprocessing
UNCHANGED threads on the recurring sweep, which is exactly what a
backfill needs to do to reach attachments that were already ingested
under the old flow. Does NOT touch is_message_seen/last_synced_at state
-- those drive the incremental recurring sweep's cursor and must not
move because of a backfill pass.

access_tier is looked up from the existing row and passed straight
through -- write_thread's ON CONFLICT clause never updates access_tier
on an existing thread anyway (so this is technically redundant with that
guard), but doing it explicitly means a backfill run is never the one
LLM-calling classify_tier() ~8000 times over data whose tier was already
decided.

Bonus effect of the full-thread rewrite (not incidental -- this is why
rewrite-the-whole-thread was chosen over a row-level UPDATE): legacy
attachment filenames from the old markdown-vault loader were stored
wrapped as "[real-name.pdf](attachments/.../real-name.pdf)", which broke
extractable()'s extension check (extension came out "pdf)"). A fresh
Gmail/Zoho API fetch produces the real, clean filename directly, so the
backfill fixes that data-quality bug for free everywhere it rewrites.

Resumable by construction: the "needs backfill" query only selects
threads with a NULL source_attachment_id attachment, so an interrupted
run picks up exactly where it left off if re-invoked, no separate
checkpoint file needed.

Usage: python -m ingestion.backfill_attachments [--account NAME] [--limit N]
       python -m ingestion.backfill_attachments --zoho
"""
import argparse
import logging
import mimetypes
import os
import re
import time

from ingestion.db import dual_write, get_live_conn
from ingestion.gmail_fetcher import get_gmail_service, fetch_thread_detail, ACCOUNTS
from ingestion.zoho_fetcher import ZohoClient, build_thread_groups, decode_zoho_body, extractable, extract_text, MAX_ATTACHMENT_DOWNLOAD_BYTES
from ingestion.write_email import write_thread
from ingestion.routing import load_client_index, classify_client
from ingestion.state import now_utc

logger = logging.getLogger("ingestion.backfill_attachments")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

# The sibling old vault repo (/home/deploy/raj-wiki-vault, file-based, read
# only -- never written to from here) still has the raw attachment files on
# disk for most threads ingested before the DB-native rewrite, organized as
# raw/emails/{account}/attachments/{gmail_thread_id}/{filename}. Checked
# live: covers 86% of the extractable-format (pdf/docx/xlsx/csv) rows that
# need backfilling. Reading these directly is a local file read instead of
# a Gmail/Zoho API round trip -- worth doing as a fast first pass before
# falling back to the API for whatever it doesn't cover (images/ics never
# match here since this vault never stored those either -- expected, not a
# gap; those still get source_attachment_id/mimetype from the API pass).
OLD_VAULT_EMAILS_DIR = "/home/deploy/raj-wiki-vault/raw/emails"

_MARKDOWN_LINK_RE = re.compile(r"^\[(.*?)\]\(.*\)$")


def _real_filename(filename):
    """Legacy rows from the old markdown-vault loader stored filename as
    "[real-name.pdf](attachments/.../real-name.pdf)" -- unwrap that back to
    the real name so extension-based extractable()/extract_text() and the
    on-disk lookup both work. A filename that was never wrapped this way
    passes through unchanged."""
    m = _MARKDOWN_LINK_RE.match(filename or "")
    return m.group(1) if m else filename


def _update_attachment(conn, *, source_account, gmail_thread_id, old_filename, real_filename, mimetype, extracted_text):
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE email_attachments a SET filename = %s, mimetype = %s, extracted_text = %s
            FROM email_threads t
            WHERE a.thread_id = t.id AND t.source_account = %s AND t.gmail_thread_id = %s AND a.filename = %s
            """,
            (real_filename, mimetype, extracted_text, source_account, gmail_thread_id, old_filename),
        )
    conn.commit()


def backfill_from_local_vault(log_every=200):
    """Fast path: fills extracted_text/mimetype (NOT source_attachment_id --
    unobtainable from a local file, only the provider API has it) for every
    extractable-format attachment whose file already exists in the old
    vault. Row-level UPDATE keyed by (source_account, gmail_thread_id, old
    filename), not thread rewrite -- much cheaper than the API path, and
    safe to run before it (the later full API backfill still visits every
    one of these threads for source_attachment_id and will simply
    re-derive the same extracted_text from a fresh download, no conflict)."""
    conn = get_live_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT t.source_account, t.gmail_thread_id, a.filename
                FROM email_attachments a JOIN email_threads t ON t.id = a.thread_id
                WHERE a.source_attachment_id IS NULL AND a.extracted_text IS NULL
            """)
            rows = cur.fetchall()
    finally:
        conn.close()

    counts = {"updated": 0, "not_on_disk": 0, "skipped_unextractable": 0, "error": 0}
    started = time.time()
    for i, row in enumerate(rows, 1):
        real_fn = _real_filename(row["filename"])
        if not extractable(real_fn):
            counts["skipped_unextractable"] += 1
            continue
        path = os.path.join(OLD_VAULT_EMAILS_DIR, row["source_account"], "attachments", row["gmail_thread_id"], real_fn)
        if not os.path.isfile(path):
            counts["not_on_disk"] += 1
            continue
        try:
            with open(path, "rb") as f:
                data = f.read()
            text = extract_text(real_fn, data)
            mimetype = mimetypes.guess_type(real_fn)[0]
            dual_write(
                _update_attachment,
                source_account=row["source_account"], gmail_thread_id=row["gmail_thread_id"],
                old_filename=row["filename"], real_filename=real_fn, mimetype=mimetype, extracted_text=text,
            )
            counts["updated"] += 1
        except Exception as e:
            logger.error("local-vault update failed for %s/%s/%s: %s", row["source_account"], row["gmail_thread_id"], real_fn, e)
            counts["error"] += 1

        if i % log_every == 0 or i == len(rows):
            elapsed = time.time() - started
            logger.info("local-vault progress %d/%d updated=%d not_on_disk=%d skipped_unextractable=%d error=%d elapsed=%.0fs",
                        i, len(rows), counts["updated"], counts["not_on_disk"], counts["skipped_unextractable"], counts["error"], elapsed)

    logger.info("local-vault backfill done: %s", counts)
    return counts


def _threads_needing_backfill(source_account, limit=None, shard_index=None, shard_count=None):
    conn = get_live_conn()
    try:
        with conn.cursor() as cur:
            sql = """
                SELECT DISTINCT t.gmail_thread_id, t.access_tier
                FROM email_threads t JOIN email_attachments a ON a.thread_id = t.id
                WHERE t.source_account = %s AND a.source_attachment_id IS NULL
                ORDER BY t.gmail_thread_id
            """
            params = [source_account]
            if limit:
                sql += " LIMIT %s"
                params.append(limit)
            cur.execute(sql, params)
            rows = cur.fetchall()
    finally:
        conn.close()
    if shard_count:
        # Deterministic partition by position in the (stable, ORDER BY'd)
        # result -- each concurrent process handles a disjoint slice of the
        # same account's threads, no coordination needed between shards.
        # Python's built-in hash() is NOT used here: it's randomized per
        # process (PYTHONHASHSEED), so two shard processes would compute
        # different bucket assignments for the same thread id and could
        # double-process or entirely skip it. Index-based slicing is
        # correct because every shard runs the identical query.
        rows = [r for i, r in enumerate(rows) if i % shard_count == shard_index]
    return rows


def backfill_gmail(account, limit=None, log_every=25, shard_index=None, shard_count=None):
    service = get_gmail_service(account)
    client_index = load_client_index()
    targets = _threads_needing_backfill(account, limit=limit, shard_index=shard_index, shard_count=shard_count)
    logger.info("account=%s shard=%s/%s threads to backfill=%d", account, shard_index, shard_count, len(targets))

    counts = {"written": 0, "gone": 0, "error": 0}
    started = time.time()
    for i, row in enumerate(targets, 1):
        thread_id = row["gmail_thread_id"]
        try:
            detail = fetch_thread_detail(service, thread_id)
            if not detail:
                logger.warning("account=%s thread=%s no longer resolvable via API (deleted?), skipping", account, thread_id)
                counts["gone"] += 1
                continue
            dual_write(
                write_thread,
                source_account=account, gmail_thread_id=detail["gmail_thread_id"],
                subject=detail["subject"], from_addr=detail["from_addr"], to_addr=detail["to_addr"],
                message_count=detail["message_count"], participants=detail["participants"],
                thread_dates=detail["thread_dates"], tags=["email", account],
                is_quarantined=False, generated_at=now_utc(), messages=detail["messages"],
                attachments_by_message_index=detail["attachments_by_message_index"],
                client_id=classify_client(client_index, detail["participants"]),
                access_tier=row["access_tier"],
            )
            counts["written"] += 1
        except Exception as e:
            logger.error("account=%s thread=%s failed: %s", account, thread_id, e)
            counts["error"] += 1

        if i % log_every == 0 or i == len(targets):
            elapsed = time.time() - started
            rate = i / elapsed if elapsed else 0
            eta_min = (len(targets) - i) / rate / 60 if rate else float("inf")
            logger.info("account=%s progress %d/%d written=%d gone=%d error=%d elapsed=%.0fs eta=%.1fmin",
                        account, i, len(targets), counts["written"], counts["gone"], counts["error"], elapsed, eta_min)

    logger.info("account=%s backfill done: %s", account, counts)
    return counts


def backfill_zoho(limit=None, log_every=5):
    conn = get_live_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT t.gmail_thread_id, t.access_tier
                FROM email_threads t JOIN email_attachments a ON a.thread_id = t.id
                WHERE t.source_account = 'support_zoho' AND a.source_attachment_id IS NULL
                ORDER BY t.gmail_thread_id
            """)
            targets = cur.fetchall()
    finally:
        conn.close()
    if limit:
        targets = targets[:limit]
    target_ids = {r["gmail_thread_id"]: r["access_tier"] for r in targets}
    logger.info("zoho threads to backfill=%d", len(targets))
    if not targets:
        return {"written": 0, "gone": 0, "error": 0}

    client = ZohoClient()
    client_index = load_client_index()
    all_messages = client.list_all_messages(0, 20000)
    groups = build_thread_groups(all_messages)
    logger.info("zoho: fetched %d historical messages, %d distinct threads total", len(all_messages), len(groups))

    counts = {"written": 0, "gone": 0, "error": 0}
    started = time.time()
    for i, (thread_id, access_tier) in enumerate(target_ids.items(), 1):
        msgs = groups.get(thread_id)
        if not msgs:
            logger.warning("zoho thread=%s not found in historical message list, skipping", thread_id)
            counts["gone"] += 1
            continue
        try:
            msgs = sorted(msgs, key=lambda m: int(m.get("receivedTime", 0)))
            msg_records = []
            attachments_by_message_index = {}
            for idx, m in enumerate(msgs):
                content = client.fetch_message_content(m["folderId"], m["messageId"])
                body = decode_zoho_body(content)
                received = int(m.get("receivedTime", 0))
                from datetime import datetime, timezone
                msg_date = datetime.fromtimestamp(received / 1000, tz=timezone.utc) if received else None
                msg_records.append({
                    "message_index": idx + 1,
                    "message_date": msg_date,
                    "from_addr": m.get("fromAddress"),
                    "body": body,
                })
                if str(m.get("hasAttachment")) == "1":
                    for att in client.fetch_attachment_info(m["folderId"], m["messageId"]):
                        size = att.get("attachmentSize")
                        try:
                            size_int = int(size)
                        except (TypeError, ValueError):
                            size_int = None
                        attachment_id = att.get("attachmentId", "")
                        filename = att.get("attachmentName") or f"attachment-{attachment_id}"
                        extracted_text = None
                        if (
                            attachment_id and extractable(filename)
                            and (size_int is None or size_int < MAX_ATTACHMENT_DOWNLOAD_BYTES)
                        ):
                            data = client.download_attachment(m["folderId"], m["messageId"], attachment_id)
                            if data is not None:
                                extracted_text = extract_text(filename, data)
                        attachments_by_message_index.setdefault(idx + 1, []).append({
                            "filename": filename,
                            "size_bytes": size_int,
                            "source_attachment_id": attachment_id or None,
                            "mimetype": mimetypes.guess_type(filename)[0],
                            "extracted_text": extracted_text,
                        })

            subject = msgs[0].get("subject", "(no subject)")
            participants = sorted({m.get("fromAddress", "").lower() for m in msgs if m.get("fromAddress")})
            dual_write(
                write_thread,
                source_account="support_zoho", gmail_thread_id=thread_id,
                subject=subject, from_addr=msgs[0].get("fromAddress"), to_addr=msgs[0].get("toAddress"),
                message_count=len(msgs), participants=participants,
                thread_dates=[m["message_date"] for m in msg_records if m["message_date"]],
                tags=["email", "support_zoho"], is_quarantined=False, generated_at=now_utc(),
                messages=msg_records, attachments_by_message_index=attachments_by_message_index,
                client_id=classify_client(client_index, participants),
                access_tier=access_tier,
            )
            counts["written"] += 1
        except Exception as e:
            logger.error("zoho thread=%s failed: %s", thread_id, e)
            counts["error"] += 1

        if i % log_every == 0 or i == len(target_ids):
            logger.info("zoho progress %d/%d written=%d gone=%d error=%d", i, len(target_ids), counts["written"], counts["gone"], counts["error"])

    logger.info("zoho backfill done: %s", counts)
    return counts


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--account", choices=list(ACCOUNTS.keys()))
    parser.add_argument("--zoho", action="store_true")
    parser.add_argument("--local-vault", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--shard-index", type=int, default=None)
    parser.add_argument("--shard-count", type=int, default=None)
    args = parser.parse_args()

    if args.local_vault:
        print(backfill_from_local_vault())
    elif args.zoho:
        print(backfill_zoho(limit=args.limit))
    elif args.account:
        print(backfill_gmail(args.account, limit=args.limit, shard_index=args.shard_index, shard_count=args.shard_count))
    else:
        results = {}
        for account in ACCOUNTS:
            results[account] = backfill_gmail(account, limit=args.limit)
        results["support_zoho"] = backfill_zoho(limit=args.limit)
        print(results)


if __name__ == "__main__":
    main()
