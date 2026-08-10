"""One-off backfill: recompute call_date for existing call_transcripts rows
using America/New_York (ET) instead of the raw UTC date -- see ingestion.tz
for why. Re-derives the correct date from a fresh list fetch against each
source's API (Fireflies' list query and Fathom's meetings list both already
carry the raw timestamp, so this is a handful of paginated list calls, not
one API call per row) and updates any row whose stored call_date disagrees.

Applies to both the live and staging databases (dual_write keeps both in
sync going forward; this corrects what's already there in both).

Usage: python -m ingestion.backfill_call_dates [--dry-run]
"""
import argparse
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ingestion.db import get_live_conn, get_staging_conn
from ingestion.fireflies_fetcher import fetch_all_stubs, ts_to_dt
from ingestion.fathom_fetcher import fetch_all_meetings, meeting_start_dt
from ingestion.tz import et_date

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("ingestion.backfill_call_dates")

FETCH_LIMIT = 5000


def build_fireflies_date_map():
    api_key = os.environ["FIREFLIES_API_KEY"]
    stubs = fetch_all_stubs(api_key, FETCH_LIMIT)
    return {s["id"]: et_date(ts_to_dt(s.get("date"))) for s in stubs if s.get("id")}


def build_fathom_date_map():
    api_key = os.environ["RON_FATHOM_API_KEY"]
    meetings = fetch_all_meetings(api_key, created_after=None, limit_override=FETCH_LIMIT)
    result = {}
    for m in meetings:
        rid = m.get("recording_id") or m.get("id")
        if rid:
            result[str(rid)] = et_date(meeting_start_dt(m))
    return result


def backfill_conn(conn, date_maps, dry_run):
    fixed = skipped_not_found = unchanged = 0
    with conn.cursor() as cur:
        cur.execute("SELECT id, source, external_id, meeting_title, call_date FROM call_transcripts")
        rows = cur.fetchall()
        for row in rows:
            date_map = date_maps.get(row["source"])
            correct = date_map.get(row["external_id"]) if date_map else None
            if correct is None:
                skipped_not_found += 1
                continue
            if correct == row["call_date"]:
                unchanged += 1
                continue
            logger.info(
                "id=%s source=%s title=%r: %s -> %s",
                row["id"], row["source"], row["meeting_title"], row["call_date"], correct,
            )
            fixed += 1
            if not dry_run:
                cur.execute("UPDATE call_transcripts SET call_date = %s WHERE id = %s", (correct, row["id"]))
    if not dry_run:
        conn.commit()
    return {"total": len(rows), "fixed": fixed, "unchanged": unchanged, "skipped_not_found": skipped_not_found}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    logger.info("fetching fireflies + fathom source-of-truth date maps...")
    date_maps = {"fireflies": build_fireflies_date_map(), "fathom": build_fathom_date_map()}
    logger.info("fireflies map=%d fathom map=%d", len(date_maps["fireflies"]), len(date_maps["fathom"]))

    for label, conn_fn in [("live", get_live_conn), ("staging", get_staging_conn)]:
        conn = conn_fn()
        try:
            stats = backfill_conn(conn, date_maps, args.dry_run)
            logger.info("%s: %s", label, stats)
        finally:
            conn.close()


if __name__ == "__main__":
    main()
