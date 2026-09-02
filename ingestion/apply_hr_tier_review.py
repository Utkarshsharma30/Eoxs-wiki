"""One-time apply step for ingestion/reclassify_hr_tier.py's downgrade
review file (reclassify_hr_tier_review.csv) -- after manual review, writes
each row's recommended_tier to its access_tier column.

Guarded: only applies a row if its access_tier is STILL 'tier2_confidential'
at apply time (WHERE access_tier = 'tier2_confidential' in the UPDATE) --
if anything else touched the row between the audit run and this apply (a
live write, a re-run of the bulk classifier), this skips it rather than
clobbering a newer classification, and reports the mismatch.
"""
import csv
import logging
import sys

from ingestion.db import get_live_conn

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("ingestion.apply_hr_tier_review")

REVIEW_CSV = "reclassify_hr_tier_review.csv"


def main():
    with open(REVIEW_CSV, newline="") as f:
        rows = list(csv.DictReader(f))
    logger.info("applying %d reviewed rows from %s", len(rows), REVIEW_CSV)

    conn = get_live_conn()
    applied = 0
    skipped = []
    try:
        with conn.cursor() as cur:
            for r in rows:
                cur.execute(
                    f"UPDATE {r['table']} SET access_tier = %s "
                    f"WHERE id = %s AND access_tier = 'tier2_confidential'",
                    (r["recommended_tier"], r["id"]),
                )
                if cur.rowcount == 1:
                    applied += 1
                else:
                    skipped.append(r)
        conn.commit()
    finally:
        conn.close()

    logger.info("applied: %d, skipped (no longer tier2_confidential): %d", applied, len(skipped))
    if skipped:
        for r in skipped:
            logger.warning("SKIPPED (tier changed since audit): %s#%s (%s)", r["table"], r["id"], r["label"])
        sys.exit(1)


if __name__ == "__main__":
    main()
