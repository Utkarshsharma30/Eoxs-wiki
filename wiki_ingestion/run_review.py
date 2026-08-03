"""Phase 5: review-sweep driver. Runs one headless sub-agent per chunk of
draft staging pages, each spot-checking citations and content quality,
then marking every page in its chunk 'reviewed' or 'rejected' via
review_mcp_server.py. Promotion to live is a deliberately separate,
human-gated step (wiki_ingestion/promote.py) -- this driver never calls
it, so a review sweep can never itself push anything live.

Chunks run sequentially, same reasoning as Phase 3/4 (2 CPU cores, each
call is a real `claude -p` process). A chunk failure never aborts the
sweep -- it's recorded and the driver moves on; any pages in that chunk
stay 'draft' for the next sweep to retry.
"""
import json

from wiki_ingestion.review import chunk_rows, find_draft_pages
from wiki_ingestion.headless_agent import run_headless_agent
from wiki_ingestion.linear_report import report_review

PROMPT_TEMPLATE = """You are a wiki-review sub-agent for eoxs-wiki-db, EOXS's second-brain database.

Below is a batch of {count} draft wiki pages pending review, written by earlier ingestion
sub-agents and already deduplicated. Your job is quality control before promotion to the live
wiki -- for EACH page in this batch:

1. Call get_staging_page(id) to read its full content (body, citations, flags, links).
2. Spot-check at least one citation per page against the real raw source (using the read tools --
   e.g. get_ticket, get_email, search_calls, get_implementation_task -- matching the citation's
   source_type/source_id) to confirm it's accurate, not fabricated or misattributed.
3. Judge whether the content is genuinely useful, well-formed wiki content -- not noise, not
   near-empty, not just restating one trivial fact -- and that any flags on the page (contradiction/
   unverified) are reasonable, not a sign the page shouldn't exist.
4. Call EXACTLY ONE of mark_reviewed(id, notes=...) or mark_rejected(id, reason=...) for every
   page id in this batch -- don't leave any unmarked. notes/reason should be specific enough that
   someone reading it later (a human deciding whether to approve promotion) understands your
   reasoning, not just "looks fine" or "looks bad".

## Pages in this batch
{row_list}

You have no file/shell access, only the MCP tools listed -- work entirely through them.

When you're done, state in plain text how many you marked reviewed vs rejected, and why for
any rejections.
"""


def _row_summary(row):
    return f"- id={row['id']} [{row['page_type']}] \"{row['title']}\""


def build_prompt(rows):
    row_list = "\n".join(_row_summary(r) for r in rows)
    return PROMPT_TEMPLATE.format(count=len(rows), row_list=row_list)


def run_review_chunk(rows, timeout_seconds=1200, max_attempts=3, retry_delay_seconds=5):
    prompt = build_prompt(rows)
    return run_headless_agent(
        "wiki_ingestion.review_mcp_server", {}, prompt,
        timeout_seconds=timeout_seconds, max_attempts=max_attempts, retry_delay_seconds=retry_delay_seconds,
    )


def run_review_sweep(timeout_seconds=1200):
    """Runs one full review sweep over every current draft page. Returns a
    summary dict; never raises -- a chunk failure is recorded and the
    driver moves on, matching run_headless_agent's never-raises contract.
    Promotion is NOT run automatically at the end of this -- see module
    docstring."""
    rows = find_draft_pages()
    chunks = chunk_rows(rows)

    results = []
    for i, chunk in enumerate(chunks):
        result = run_review_chunk(chunk, timeout_seconds=timeout_seconds)
        entry = {"chunk_index": i, "page_ids": [r["id"] for r in chunk], "ok": result["ok"]}
        if not result["ok"]:
            entry["error"] = (result.get("stderr") or "")[-2000:] or f"nonzero exit {result.get('returncode')}"
        results.append(entry)

    result = {
        "drafts_total": len(rows),
        "chunks_total": len(chunks),
        "chunks_failed": sum(1 for r in results if not r["ok"]),
        "results": results,
    }
    report_review(result)
    return result


if __name__ == "__main__":
    result = run_review_sweep()
    print(json.dumps(result, indent=2))
