"""Recurring wiki-ingestion pipeline: Phase 3 (detect+ingest) -> Phase 4
(consolidate) -> Phase 5 (review), run sequentially as one call. This is
what deploy/eoxs-wiki-pipeline.timer invokes every 6 hours -- the
"6-hourly review sweep" cadence from the confirmed phase plan.

Deliberately stops after review. Promotion (wiki_ingestion/promote.py)
is NOT called here -- explicit instruction: promotion stays human-gated
for now, reviewed pages wait for someone to call
promote.promote_reviewed_pages() on purpose. Wire that in here (or a
separate timer) once that trust is established.

Consolidation and review run over ALL current staging drafts, not just
this run's new cycle -- same as when run standalone -- so a draft that
missed consolidation/review in an earlier run (e.g. it arrived between a
previous run's Phase 4 and Phase 5) still gets picked up next time.

Each phase's own driver already handles its failures internally (a
batch/chunk/group failure never raises) and reports to Linear on EDB
(wiki_ingestion/linear_report.py) -- this orchestrator doesn't add
another layer of error handling on top, just calls them in order.
"""
import json

from wiki_ingestion.run_cycle import run_cycle
from wiki_ingestion.run_consolidation import run_consolidation_pass
from wiki_ingestion.run_review import run_review_sweep


def run_pipeline(timeout_seconds=1200):
    cycle_result = run_cycle(timeout_seconds=timeout_seconds)
    consolidation_result = run_consolidation_pass(timeout_seconds=timeout_seconds)
    review_result = run_review_sweep(timeout_seconds=timeout_seconds)
    return {
        "cycle": cycle_result,
        "consolidation": consolidation_result,
        "review": review_result,
    }


if __name__ == "__main__":
    result = run_pipeline()
    print(json.dumps(result, indent=2))
