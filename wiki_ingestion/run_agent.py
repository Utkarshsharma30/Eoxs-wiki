"""Phase 3: headless sub-agent invocation for the wiki-ingestion pipeline.
One call per category batch (a chunk from a detect.py partition), each an
isolated `claude -p` process whose only interface to the world is
wiki_ingestion/agent_mcp_server.py.

The actual `claude -p` invocation mechanics (deny-list, retries, MCP
config) live in headless_agent.py, shared with Phase 4's consolidation
sub-agent -- see that module's docstring for the deny-list-length bug
this was built around.
"""
from wiki_ingestion.headless_agent import run_headless_agent, WIKI_MCP_BASE_URL

PROMPT_TEMPLATE = """You are a wiki-ingestion sub-agent for eoxs-wiki-db, EOXS's second-brain database.

Your job: read the raw source data listed below (category: {source_kind}), and write curated,
synthesized wiki pages to the staging area via your tools -- summarizing what's genuinely
new or noteworthy, cross-referencing related people/companies/topics, and citing the exact
raw source rows you drew from.

## Candidate rows in this batch ({row_count} total)
{row_list}
{category_notes}
## Rules
- ALWAYS call search_wiki_inventory before creating a page, to check whether a page on this
  topic already exists (live) or is already being drafted (staging) -- prefer update_staging_page
  over create_staging_page whenever a matching page already exists.
- Use the read tools (search_emails, get_ticket, search_calls, etc.) to pull full context on
  each candidate row before writing about it -- do not write from the row summary alone.
- Every page you write must cite its sources via add_staging_citation.
- Cross-reference related pages via add_staging_link where genuinely relevant -- don't force it.
- Flag contradictions you notice between sources via add_staging_flag(flag_type='contradiction'),
  and claims you can't fully verify via add_staging_flag(flag_type='unverified').
- Skip rows that are pure noise (automated notifications, spam-adjacent content that survived
  upstream filtering, near-duplicate content already covered) -- don't force a page for
  everything, only for what's genuinely worth capturing.
- You may create/update MULTIPLE pages in this batch if the candidates span multiple topics.
- You have no file/shell access, only the MCP tools listed -- work entirely through them.

When you're done, state in plain text: how many pages you created/updated, and what you
deliberately skipped and why.
"""

CATEGORY_NOTES = {
    "repo_docs": """
## Notes for this batch (category: repo_docs)
These rows are this repository's OWN documentation, not EOXS business content -- this
repo's `docs/*.md` files, `ARCHITECTURE.md`, or the synthesized codebase-overview
document. Treat each one as a real, worth-capturing topic by default (this is curated
engineering reference material someone deliberately wrote, not raw noisy correspondence
to filter) -- do not skip a row here for being "not noteworthy" the way you might skip a
routine notification email.
- Use get_repo_doc(identifier) with the row's `id` or `slug` to read the FULL current text
  before drafting -- the row summary below is a title only.
- Write one wiki page per repo doc (1:1 is expected and normal here, unlike email/call
  batches where many rows can collapse into one page) -- update the existing page via
  update_staging_page if this doc already has one (search_wiki_inventory first, as always),
  since these get re-synced on every edit and should refresh in place, not fork into
  duplicate pages.
- **You MUST cite every repo_docs row via `add_staging_citation(..., source_type='repo_doc',
  source_id=<repo_docs.id>, ...)` -- never 'asset' or any other source_type for these.**
  Every repo_docs row is hardcoded tier1 (Raj-only), and access-tier promotion computes a
  page's final tier from its citations' source_types (see promote.py) -- citing a repo_doc
  under the wrong source_type means the page silently promotes to the wrong (less
  restrictive) tier, exposing internal engineering/infra detail past `full`/`ayan`. If you
  cite a repo_doc row, `source_type='repo_doc'` is not optional.
""",
}


def _category_notes(source_kind):
    return CATEGORY_NOTES.get(source_kind, "")


def _row_summary(row, source_kind):
    if source_kind == "tickets":
        return f"- ticket id={row['id']} {row.get('ticket_number', '')}: {row.get('subject', '')}"
    if source_kind.startswith("client_"):
        # odoo_task_id, not the volatile serial `id` -- implementation_tasks gets a full
        # DELETE+INSERT every raw-ingestion sweep, so `id` can go stale by the time this
        # gets cited/reviewed hours later. get_implementation_task resolves either, but
        # only odoo_task_id survives a table refresh -- always cite that one.
        return f"- implementation task odoo_task_id={row['odoo_task_id']}: {row.get('task_name', '')} (stage: {row.get('stage', '')})"
    if source_kind == "calls":
        return f"- call id={row['id']} ({row.get('source', '')}): {row.get('meeting_title', '')}"
    if source_kind == "assets":
        return f"- asset id={row['id']} slug={row.get('slug', '')}: {row.get('title', '')}"
    if source_kind == "repo_docs":
        return f"- repo doc id={row['id']} slug={row.get('slug', '')}: {row.get('title', '')}"
    return f"- email thread id={row['id']}: {row.get('subject', '')}"  # email accounts


def build_prompt(source_kind, rows):
    row_list = "\n".join(_row_summary(r, source_kind) for r in rows)
    return PROMPT_TEMPLATE.format(
        source_kind=source_kind, row_count=len(rows), row_list=row_list,
        category_notes=_category_notes(source_kind),
    )


def run_agent(cycle_id, source_kind, rows, timeout_seconds=1200, max_attempts=5, retry_delay_seconds=10):
    """Runs one headless claude -p invocation for this category batch.
    Returns {ok, returncode, stdout, stderr, attempts} -- see
    headless_agent.run_headless_agent for the never-raises contract."""
    prompt = build_prompt(source_kind, rows)
    url = f"{WIKI_MCP_BASE_URL}/wiki-agent/{cycle_id}/{source_kind}/sse"
    return run_headless_agent(
        url, prompt,
        timeout_seconds=timeout_seconds, max_attempts=max_attempts, retry_delay_seconds=retry_delay_seconds,
    )
