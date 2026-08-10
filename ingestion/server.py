"""FastAPI ingestion server -- webhook-triggered raw ingestion (Gmail Pub/Sub,
Fireflies, Fathom Svix) plus a callable full-sweep function for the daily
cron fallback. Signature verification schemes ported from the old
pipeline's tools/webhook_server.py (same HMAC/Svix logic, real reference
for "what actually works" per HANDOFF.md's verification process) -- this
server does NOT reuse that file's git-commit/push logic, since writes here
go straight to Postgres via dual_write(), not a markdown file + git push.

Each webhook handler calls only the relevant fetcher(s) for that source
(not a blanket full-sweep like the old pipeline) -- Gmail webhooks fetch
all 3 Gmail accounts (Pub/Sub push doesn't cheaply map emailAddress to a
specific account without hardcoding real addresses, and a fetch is cheap/
idempotent via cursor+dedup when there's nothing new), Fireflies/Fathom
webhooks fetch just that source. Zoho and Odoo have no webhook in this
system (same as the old pipeline) -- they, and everything else, get
picked up by the daily full-sweep cron fallback.

This module is the ingestion server's CODE. Actually exposing it to the
public internet (reverse proxy, TLS, DNS, registering the webhook URLs
with Google Cloud Pub/Sub / Fireflies / Fathom's dashboards) is a
separate infrastructure step, not done here -- see the module docstring
note in HANDOFF.md discussions; that needs explicit user coordination
since it's shared/external-facing infra, not just code.

Usage:
    python -m ingestion.server                  # runs the FastAPI app
    python -m ingestion.server --sweep           # runs a one-shot full sweep (for cron)
"""
import argparse
import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from ingestion.gmail_fetcher import process_account as gmail_process_account, ACCOUNTS as GMAIL_ACCOUNTS
from ingestion.zoho_fetcher import process_zoho
from ingestion.fireflies_fetcher import process_fireflies
from ingestion.fathom_fetcher import process_fathom
from ingestion.odoo_fetcher import process_all as odoo_process_all
from ingestion.tickets_fetcher import process_tickets
from ingestion.invoice_fetcher import process_invoices
from ingestion.ingest_log import log_run
from ingestion.linear_report import start_sweep_parent, finish_sweep_parent, start_source_task, finish_source_task
from ingestion.state import now_utc

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("ingestion.server")

FIREFLIES_WEBHOOK_SECRET = os.environ.get("FIREFLIES_WEBHOOK_SECRET", "")
FATHOM_WEBHOOK_SECRET = os.environ.get("FATHOM_WEBHOOK_SECRET", "")
WEBHOOK_SECRET = os.environ.get("INGESTION_WEBHOOK_SECRET", "")

app = FastAPI(title="eoxs-wiki-db Raw Ingestion Server", version="1.0.0")

_sweep_lock = asyncio.Lock()
_last_run = {"time": None, "status": None, "source": None, "counts": None}


def _verify_svix(secret, svix_id, svix_timestamp, body, svix_signature):
    """Verifies a Svix webhook signature (whsec_... format) -- ported
    verbatim from the old pipeline's tools/webhook_server.py."""
    try:
        key = base64.b64decode(secret.split("_", 1)[1] + "==")
    except Exception:
        return False
    signed = f"{svix_id}.{svix_timestamp}.".encode() + body
    digest = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
    expected = f"v1,{digest}"
    for part in svix_signature.split():
        if hmac.compare_digest(part.strip(), expected):
            return True
    return False


def run_gmail_all():
    counts = {}
    for account in GMAIL_ACCOUNTS:
        try:
            counts[account] = gmail_process_account(account)
        except Exception as e:
            logger.error("gmail account=%s failed: %s", account, e)
            counts[account] = {"error": str(e)}
    return counts


def run_full_sweep():
    """Every source, once. Used by the daily cron fallback and /trigger/manual.

    Reports granularly to Linear as it goes (2026-08 redesign): one parent
    issue for the whole sweep, created before the first source runs, and
    one child task per source, created right before that source's fetch
    call and updated immediately after with its real outcome -- not one
    aggregate issue built after everything already finished. Reporting is
    unconditional here now (moved out of the caller-controlled
    report_to_linear flag in _run_bg/main, since every current caller of
    run_full_sweep() wants it)."""
    run_at = now_utc()
    parent_issue_id = start_sweep_parent()
    summary = {}
    for name, fn in [
        ("gmail", run_gmail_all),
        ("zoho", process_zoho),
        ("fireflies", process_fireflies),
        ("fathom", process_fathom),
        ("odoo", odoo_process_all),
        ("tickets", process_tickets),
        ("invoices", process_invoices),
    ]:
        task_issue_id = start_source_task(parent_issue_id, name)
        try:
            summary[name] = fn()
        except Exception as e:
            logger.error("sweep source=%s failed: %s", name, e)
            summary[name] = {"error": str(e)}
        finish_source_task(task_issue_id, name, summary[name])
    finish_sweep_parent(parent_issue_id, summary, run_at)
    return summary


async def _run_bg(trigger_source, fn, *args, **kwargs):
    """Linear reporting for full sweeps now happens INSIDE run_full_sweep()
    itself (granular, per-source), not here -- individual-source webhook
    triggers (gmail/fireflies/fathom) call their own fetcher directly, not
    run_full_sweep(), so they still never touch Linear (a single new Gmail
    message shouldn't flood the EDB board). ingest_log.log_run still
    records every trigger, webhook or sweep, regardless."""
    if _sweep_lock.locked():
        logger.info("Run already in progress -- skipping duplicate trigger from %s", trigger_source)
        return
    async with _sweep_lock:
        logger.info("Run triggered by: %s", trigger_source)
        _last_run["time"] = datetime.now(timezone.utc).isoformat()
        _last_run["source"] = trigger_source
        loop = asyncio.get_event_loop()
        try:
            counts = await loop.run_in_executor(None, lambda: fn(*args, **kwargs))
            _last_run["status"] = "ok"
            _last_run["counts"] = counts
            log_run(trigger_source, counts)
        except Exception as e:
            logger.error("run triggered by %s failed: %s", trigger_source, e)
            _last_run["status"] = "error"
            _last_run["counts"] = {"error": str(e)}
        logger.info("Run complete. status=%s", _last_run["status"])


@app.get("/health")
async def health():
    return {"status": "ok", "run_in_progress": _sweep_lock.locked(), "last_run": _last_run}


@app.post("/webhook/gmail")
async def gmail_webhook(request: Request, background_tasks: BackgroundTasks):
    """Gmail Pub/Sub push: a base64-encoded JSON envelope with
    {emailAddress, historyId}. Always returns 200 immediately so Pub/Sub
    doesn't retry endlessly -- actual processing happens in the background."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    message = body.get("message", {})
    data_b64 = message.get("data", "")
    try:
        data = json.loads(base64.b64decode(data_b64 + "==").decode("utf-8"))
    except Exception:
        data = {}

    email_address = data.get("emailAddress", "unknown")
    logger.info("Gmail push -- account: %s", email_address)

    background_tasks.add_task(_run_bg, f"gmail:{email_address}", run_gmail_all)
    return JSONResponse({"status": "accepted"}, status_code=200)


@app.post("/webhook/fireflies")
async def fireflies_webhook(request: Request, background_tasks: BackgroundTasks):
    """Fireflies transcript.completed webhook. Verifies HMAC-SHA256 when
    FIREFLIES_WEBHOOK_SECRET is set."""
    raw_body = await request.body()

    if FIREFLIES_WEBHOOK_SECRET:
        sig_header = request.headers.get("x-hub-signature-256", "")
        expected = "sha256=" + hmac.new(FIREFLIES_WEBHOOK_SECRET.encode(), raw_body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig_header, expected):
            logger.warning("Fireflies webhook signature mismatch -- rejected")
            raise HTTPException(status_code=401, detail="Invalid signature")

    try:
        payload = json.loads(raw_body)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    transcript_id = payload.get("transcriptId") or payload.get("id", "unknown")
    logger.info("Fireflies webhook -- transcriptId: %s", transcript_id)

    background_tasks.add_task(_run_bg, f"fireflies:{transcript_id}", process_fireflies)
    return JSONResponse({"status": "accepted"}, status_code=200)


@app.post("/webhook/fathom")
async def fathom_webhook(request: Request, background_tasks: BackgroundTasks):
    """Fathom recording.completed webhook. Verifies Svix HMAC-SHA256 when
    FATHOM_WEBHOOK_SECRET is set."""
    raw_body = await request.body()

    if FATHOM_WEBHOOK_SECRET:
        svix_id = request.headers.get("svix-id", "")
        svix_timestamp = request.headers.get("svix-timestamp", "")
        svix_signature = request.headers.get("svix-signature", "")
        if not _verify_svix(FATHOM_WEBHOOK_SECRET, svix_id, svix_timestamp, raw_body, svix_signature):
            logger.warning("Fathom webhook Svix signature mismatch -- rejected")
            raise HTTPException(status_code=401, detail="Invalid signature")

    try:
        payload = json.loads(raw_body)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    recording_id = payload.get("recording_id") or payload.get("id", "unknown")
    logger.info("Fathom webhook -- recording_id: %s", recording_id)

    background_tasks.add_task(_run_bg, f"fathom:{recording_id}", process_fathom)
    return JSONResponse({"status": "accepted"}, status_code=200)


@app.post("/trigger/manual")
async def manual_trigger(request: Request, background_tasks: BackgroundTasks):
    """Manually trigger a full sweep. Pass INGESTION_WEBHOOK_SECRET as:
    Authorization: Bearer <secret>"""
    if WEBHOOK_SECRET:
        auth = request.headers.get("Authorization", "")
        if auth != f"Bearer {WEBHOOK_SECRET}":
            raise HTTPException(status_code=401, detail="Unauthorized")

    logger.info("Manual full-sweep trigger received.")
    background_tasks.add_task(_run_bg, "manual", run_full_sweep)
    return JSONResponse({"status": "sweep started"}, status_code=202)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sweep", action="store_true", help="Run a one-shot full sweep and exit (for cron)")
    args = parser.parse_args()

    if args.sweep:
        summary = run_full_sweep()
        logger.info("cron sweep done: %s", summary)
        # NOTE: this is the actual 2-hourly production path (deploy/eoxs-sweep.timer
        # runs `python -m ingestion.server --sweep` directly, not through the FastAPI
        # app) -- previously it only logged via Python logging and never called
        # log_run/report_full_sweep at all, so ingest_log/EDB never saw these runs.
        # Linear reporting now happens granularly INSIDE run_full_sweep() itself.
        log_run("cron", summary)
        return

    import uvicorn
    port = int(os.environ.get("INGESTION_SERVER_PORT", "8090"))
    logger.info("Starting eoxs-wiki-db ingestion server on port %d", port)
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    main()
