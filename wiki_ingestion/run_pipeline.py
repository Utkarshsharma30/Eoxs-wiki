"""Recurring wiki-ingestion pipeline: repo-docs sync -> Phase 3 (detect+ingest)
-> Phase 4 (consolidate) -> Phase 5 (review) -> promotion, run sequentially as
one call. This is what deploy/eoxs-wiki-pipeline.timer invokes every 6 hours
-- the "6-hourly review sweep" cadence from the confirmed phase plan.

2026-08-28 change: import_repo_docs.import_all() now runs first, every cycle.
Previously repo_docs (docs/*.md, ARCHITECTURE.md, the synthesized
codebase-overview) only ever got a new updated_at when someone remembered to
run `python -m ingestion.import_repo_docs` by hand -- detect.py's
candidates_repo_docs() partition existed but had nothing to detect unless
that manual step happened first. Re-running the importer here (safe: it's a
pure upsert, same content produces the same row and no new updated_at) means
an edit to any of those files now flows into the wiki exactly like any other
raw source, on the next scheduled cycle, with zero manual step. Wrapped in
try/except specifically so a bug in the importer (e.g. a bad doc file) can
never block real ingestion -- the rest of the pipeline runs regardless.

2026-08 change: run_review_sweep() now calls promote_reviewed_pages()
itself at the end of every sweep (see wiki_ingestion/run_review.py and
promote.py) -- reviewed pages go live automatically, no separate wiring
needed here. Rejected pages are never touched by promotion; they still
require a human decision.

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
import traceback

from ingestion.import_repo_docs import import_all as sync_repo_docs
from wiki_ingestion.run_cycle import run_cycle
from wiki_ingestion.run_consolidation import run_consolidation_pass
from wiki_ingestion.run_review import run_review_sweep


def run_pipeline(timeout_seconds=1200):
    try:
        repo_docs_result = {"ok": True, "imported": sync_repo_docs()}
    except Exception:
        repo_docs_result = {"ok": False, "error": traceback.format_exc()}

    cycle_result = run_cycle(timeout_seconds=timeout_seconds)
    consolidation_result = run_consolidation_pass(timeout_seconds=timeout_seconds)
    review_result = run_review_sweep(timeout_seconds=timeout_seconds)
    return {
        "repo_docs_sync": repo_docs_result,
        "cycle": cycle_result,
        "consolidation": consolidation_result,
        "review": review_result,
    }


if __name__ == "__main__":
    result = run_pipeline()
    print(json.dumps(result, indent=2))
