"""Headless sub-agent invocation for the wiki-ingestion pipeline. One
call per category batch (a partition from detect.py), each an isolated
`claude -p` process whose only interface to the world is
wiki_ingestion/agent_mcp_server.py.

Tool restriction, found by testing rather than trusting the docs:
`--tools ""` (documented as "disable all tools") and `--allowedTools
"mcp__wiki_staging__*"` (an allowlist) both left every standard built-in
tool (Bash, Read, Write, Edit, ...) callable in this environment --
verified live, not assumed. `--disallowedTools` DOES work -- but a LONG
deny-list (~30 names) reliably broke MCP tool registration for complex,
multi-step prompts specifically: the model's first turn would run with
zero tools available (confirmed via --output-format stream-json's
system.init event showing "tools":[] and the MCP server stuck at
status "pending"), so it wrote out a fake text-only "tool call" instead
of a real one and stopped. This looked at first like a random MCP-
startup race (retries with the long list failed 10/10 even with backoff
delay), but removing --disallowedTools entirely fixed it deterministically,
and a SHORT deny-list (tested up to 11 names covering every genuinely
risky built-in: Bash, Write, Edit, Read, Task, Agent, WebFetch, WebSearch,
Glob, Grep, NotebookEdit) works reliably too. The actual cause is
disallow-list SIZE interacting badly with MCP tool registration on
complex prompts, not connection timing -- confirmed by holding the
prompt constant and varying only the deny-list.

Left unrestricted (harmless -- session/harness-management tools with no
path to touching this system's data or files): TodoWrite, BashOutput,
KillShell, ScheduleWakeup, ReportFindings, Skill, ToolSearch, Workflow,
and the Cron/Task-tracking/SendMessage meta-tools.
"""
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PYTHON = str(REPO_ROOT / ".venv" / "bin" / "python3")

# Every genuinely risky built-in tool (file access, shell execution,
# sub-agent spawning, network egress) -- verified short enough to not
# break MCP tool registration (see module docstring).
DISALLOWED_BUILTIN_TOOLS = "Bash Write Edit Read Task Agent WebFetch WebSearch Glob Grep NotebookEdit"

PROMPT_TEMPLATE = """You are a wiki-ingestion sub-agent for eoxs-wiki-db, EOXS's second-brain database.

Your job: read the raw source data listed below (category: {source_kind}), and write curated,
synthesized wiki pages to the staging area via your tools -- summarizing what's genuinely
new or noteworthy, cross-referencing related people/companies/topics, and citing the exact
raw source rows you drew from.

## Candidate rows in this batch ({row_count} total)
{row_list}

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


def _build_mcp_config(cycle_id, source_kind):
    config = {
        "mcpServers": {
            "wiki_staging": {
                "command": PYTHON,
                "args": ["-m", "wiki_ingestion.agent_mcp_server"],
                "cwd": str(REPO_ROOT),
                "env": {"WIKI_CYCLE_ID": str(cycle_id), "WIKI_SOURCE_KIND": source_kind},
            }
        }
    }
    fd, path = tempfile.mkstemp(suffix=".json", prefix="wiki_mcp_config_")
    with os.fdopen(fd, "w") as f:
        json.dump(config, f)
    return path


def _row_summary(row, source_kind):
    if source_kind == "tickets":
        return f"- ticket id={row['id']} {row.get('ticket_number', '')}: {row.get('subject', '')}"
    if source_kind.startswith("client_"):
        return f"- implementation task id={row['id']}: {row.get('task_name', '')} (stage: {row.get('stage', '')})"
    if source_kind == "calls":
        return f"- call id={row['id']} ({row.get('source', '')}): {row.get('meeting_title', '')}"
    return f"- email thread id={row['id']}: {row.get('subject', '')}"  # email accounts


def build_prompt(source_kind, rows):
    row_list = "\n".join(_row_summary(r, source_kind) for r in rows)
    return PROMPT_TEMPLATE.format(source_kind=source_kind, row_count=len(rows), row_list=row_list)


def _invoke_once(cycle_id, source_kind, prompt, timeout_seconds):
    mcp_config_path = _build_mcp_config(cycle_id, source_kind)
    try:
        proc = subprocess.run(
            [
                "claude", "-p", prompt,
                "--mcp-config", mcp_config_path,
                "--strict-mcp-config",
                "--disallowedTools", DISALLOWED_BUILTIN_TOOLS,
                "--permission-mode", "bypassPermissions",
                "--output-format", "json",
            ],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            stdin=subprocess.DEVNULL,
        )
    finally:
        os.unlink(mcp_config_path)
    return proc


def _looks_like_mcp_startup_race(proc):
    """num_turns<=1 with stop_reason=end_turn on a prompt that requires
    tool use to do anything useful. Should be rare now that the deny-list
    is short (see module docstring), but kept as a safety net -- retrying
    is cheap and this failure mode is unambiguous to detect."""
    if proc.returncode != 0:
        return False
    try:
        data = json.loads(proc.stdout)
    except (json.JSONDecodeError, ValueError):
        return False
    return data.get("num_turns", 99) <= 1 and data.get("stop_reason") == "end_turn"


def run_agent(cycle_id, source_kind, rows, timeout_seconds=1200, max_attempts=3, retry_delay_seconds=5):
    """Runs one headless claude -p invocation for this category batch.
    Returns {ok, returncode, stdout, stderr, attempts} -- ok is False on
    a nonzero exit, a timeout, or exhausting all retries on the
    startup-race signature above; never raises (a category-batch failure
    shouldn't crash the whole cycle; the caller records it and moves on)."""
    prompt = build_prompt(source_kind, rows)
    last_proc = None
    for attempt in range(1, max_attempts + 1):
        try:
            proc = _invoke_once(cycle_id, source_kind, prompt, timeout_seconds)
        except subprocess.TimeoutExpired as e:
            return {"ok": False, "returncode": None, "stdout": e.stdout or "", "stderr": f"timed out after {timeout_seconds}s", "attempts": attempt}
        last_proc = proc
        if proc.returncode == 0 and not _looks_like_mcp_startup_race(proc):
            return {"ok": True, "returncode": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr, "attempts": attempt}
        if attempt < max_attempts:
            time.sleep(retry_delay_seconds)
    return {"ok": False, "returncode": last_proc.returncode, "stdout": last_proc.stdout, "stderr": last_proc.stderr, "attempts": max_attempts}
