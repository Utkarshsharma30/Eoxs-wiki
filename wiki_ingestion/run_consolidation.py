"""Phase 4: consolidation driver. Runs one headless sub-agent per
duplicate-title group (consolidate.find_duplicate_groups()), each merging
its group's drafts into a single staging page via
consolidate_mcp_server.py's merge_staging_pages tool.

Groups run sequentially, same reasoning as Phase 3's chunk loop (2 CPU
cores, each call is a real `claude -p` process). A group failure never
aborts the pass -- it's recorded and the driver moves on to the next
group; a failed merge just leaves that group's duplicates as-is for the
next consolidation pass to retry.
"""
import json

from wiki_ingestion.consolidate import find_duplicate_groups
from wiki_ingestion.headless_agent import run_headless_agent, WIKI_MCP_BASE_URL
from wiki_ingestion.linear_report import (
    start_consolidation_parent, finish_consolidation_parent,
    start_consolidation_task, finish_consolidation_task,
)

PROMPT_TEMPLATE = """You are a wiki-consolidation sub-agent for eoxs-wiki-db, EOXS's second-brain database.

{count} staging drafts share the exact title "{title}" (staging page ids: {ids}) -- they were
written independently by separate ingestion sub-agents processing different batches of raw
data, and need to be merged into ONE page.

## Steps
1. Call get_staging_page for EACH of these ids to read their full content (body, citations,
   flags, links).
2. Synthesize ONE merged body: deduplicate overlapping content, preserve genuinely distinct
   sub-topics or details each draft captured that the others didn't, and note any real
   contradictions between the drafts as a new flag rather than silently picking one version.
3. Call merge_staging_pages EXACTLY ONCE: pick one of the ids as keep_id (whichever has the
   most complete/best-organized starting content), pass the rest as duplicate_ids, and pass
   your synthesized body as merged_body. Merge tags/sources_raw across all drafts too.
4. You have no file/shell access, only the MCP tools listed -- work entirely through them.

When you're done, state in plain text which id you kept and a one-line summary of what the
merge combined.
"""


def build_prompt(group):
    ids = group["page_ids"]
    return PROMPT_TEMPLATE.format(title=group["title"], ids=", ".join(str(i) for i in ids), count=len(ids))


def run_consolidation_agent(group, timeout_seconds=900, max_attempts=5, retry_delay_seconds=10):
    prompt = build_prompt(group)
    url = f"{WIKI_MCP_BASE_URL}/wiki-consolidate/sse"
    return run_headless_agent(
        url, prompt,
        timeout_seconds=timeout_seconds, max_attempts=max_attempts, retry_delay_seconds=retry_delay_seconds,
    )


def run_consolidation_pass(timeout_seconds=900):
    """Runs one full consolidation pass over every current duplicate-title
    group. Returns a summary dict; never raises -- a group failure is
    recorded and the driver moves on, matching run_headless_agent's
    never-raises contract."""
    groups = find_duplicate_groups()
    parent_issue_id = start_consolidation_parent(len(groups)) if groups else None

    results = []
    for group in groups:
        task_issue_id = start_consolidation_task(parent_issue_id, group)
        result = run_consolidation_agent(group, timeout_seconds=timeout_seconds)
        finish_consolidation_task(task_issue_id, group, result)
        entry = {"title": group["title"], "page_ids": group["page_ids"], "ok": result["ok"]}
        if not result["ok"]:
            entry["error"] = (result.get("stderr") or "")[-2000:] or f"nonzero exit {result.get('returncode')}"
        results.append(entry)
    result = {
        "groups_total": len(groups),
        "groups_failed": sum(1 for r in results if not r["ok"]),
        "results": results,
    }
    if parent_issue_id:
        finish_consolidation_parent(parent_issue_id, result)
    return result


if __name__ == "__main__":
    result = run_consolidation_pass()
    print(json.dumps(result, indent=2))
