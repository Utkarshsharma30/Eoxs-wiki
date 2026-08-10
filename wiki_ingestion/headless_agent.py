"""Shared plumbing for headless `claude -p` sub-agent invocations, used by
both Phase 3 (run_agent.py, one call per raw-data chunk) and Phase 4
(run_consolidation.py, one call per duplicate-title group). Factored out
of run_agent.py so the deny-list-length behavior below -- hard-won by
extensive live testing, not documented anywhere -- has exactly one place
to live instead of two call sites that could silently drift apart.

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

2026-08 incident -- a second, more damaging failure mode, found by
directly reproducing it live (not inferred): the "wiki" MCP server
sometimes never leaves connection status "pending" for an entire
session, confirmed via --output-format stream-json's system.init event
(`"mcp_servers":[{"name":"wiki","status":"pending"}]`, never updated
again) even though the exact same server process answers a raw MCP
`initialize` handshake correctly in well under a second when spawned
directly (outside `claude -p`) -- so the server itself is healthy, this
is specific to `claude -p`'s own MCP connection handling. Reproduced
deterministically (4/4 attempts) and independent of environment
(reproduces even under `env -i` with everything but PATH/HOME
stripped), so it is not the original short-single-turn race the
deny-list fix above addresses, and not caused by this being tested from
inside a nested Claude Code session. The sub-agent, correctly, notices
it has no tools and explains so in its final text instead of
fabricating content -- but it does this over several real turns
(num_turns > 1), so `_looks_like_mcp_startup_race`'s original check
never caught it, and the batch was recorded `ok: True`. Real-world
impact: 8 consecutive review cycles (Aug 4-6) re-reviewed the same 4
already-drafted pages and reported success without ever calling
mark_reviewed/mark_rejected, and the Phase 3 drafting step produced
zero new or updated wiki_staging pages for ~2 days despite processing
real new raw rows every cycle. Fixed below by checking whether any
mcp__wiki__* tool was actually invoked during the session -- a much
stronger signal than exit code or turn count, since run_agent.py's own
prompt requires calling search_wiki_inventory before anything else, so
a real successful session (even one that legitimately decides to draft
nothing) always uses at least one wiki tool.

2026-08 root-level fix for the incident above: rather than spawning a
fresh stdio MCP subprocess per invocation (the unreliable part), every
call now connects over HTTP/SSE to wiki_ingestion/mcp_http_server.py, one
always-on persistent server (systemd-managed, eoxs-wiki-mcp.service) --
the same transport pattern mcp_server/http_server.py already uses
reliably for the external claude.ai connector. See mcp_http_server.py's
module docstring for the full rationale. server_module/env (a stdio
subprocess command + its environment) are replaced by a single `url`
argument -- the caller builds the right URL for its phase (agent calls
need cycle_id/source_kind baked into the path; review/consolidation
don't need any per-call scoping).
"""
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

WIKI_MCP_HTTP_PORT = int(os.environ.get("WIKI_MCP_HTTP_PORT", "8093"))
WIKI_MCP_BASE_URL = f"http://127.0.0.1:{WIKI_MCP_HTTP_PORT}"

# Every genuinely risky built-in tool (file access, shell execution,
# sub-agent spawning, network egress) -- verified short enough to not
# break MCP tool registration (see module docstring).
DISALLOWED_BUILTIN_TOOLS = "Bash Write Edit Read Task Agent WebFetch WebSearch Glob Grep NotebookEdit"


def _build_mcp_config(url):
    config = {"mcpServers": {"wiki": {"type": "sse", "url": url}}}
    fd, path = tempfile.mkstemp(suffix=".json", prefix="wiki_mcp_config_")
    with os.fdopen(fd, "w") as f:
        json.dump(config, f)
    return path


def _invoke_once(url, prompt, timeout_seconds):
    mcp_config_path = _build_mcp_config(url)
    try:
        proc = subprocess.run(
            [
                "claude", "-p", prompt,
                "--mcp-config", mcp_config_path,
                "--strict-mcp-config",
                "--disallowedTools", DISALLOWED_BUILTIN_TOOLS,
                "--permission-mode", "bypassPermissions",
                "--output-format", "stream-json",
                "--verbose",
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


def _parse_stream_json(stdout):
    """--output-format stream-json emits one JSON object per line, not a
    single blob. Returns (result_event, used_wiki_tool): result_event is
    the final {"type": "result", ...} event -- same shape the old
    --output-format json gave directly, so callers reading e.g.
    is_error/num_turns off it are unaffected. used_wiki_tool is True iff
    the session actually invoked at least one mcp__wiki__* tool (the
    server is always registered under the config key "wiki" -- see
    _build_mcp_config -- regardless of which phase's server_module is
    in use, so this check is identical across Phase 3/4/5)."""
    result_event = None
    used_wiki_tool = False
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if event.get("type") == "result":
            result_event = event
        elif event.get("type") == "assistant":
            for block in (event.get("message") or {}).get("content") or []:
                if isinstance(block, dict) and str(block.get("name", "")).startswith("mcp__wiki__"):
                    used_wiki_tool = True
    return result_event, used_wiki_tool


def _looks_like_mcp_startup_race(result_event, used_wiki_tool):
    """True if the wiki MCP server's tools were never actually used --
    catches both the original short-single-turn startup race AND the
    'server stuck at connection status pending for the whole session,
    model gives up after several turns and explains it in plain text
    instead of fabricating content' failure mode found 2026-08 (see
    module docstring's incident note). A batch that never touches a
    single wiki tool did not do its job, no matter how cleanly the
    process exited or how many turns it took to give up. Every phase's
    prompt requires calling a wiki tool before anything else (Phase 3:
    search_wiki_inventory, Phase 4/5: get_staging_page), so a
    legitimately successful session -- even one that ends up drafting/
    changing nothing -- always uses at least one."""
    if result_event is None:
        return True  # couldn't even find a result event -- treat as failure, not silent success
    if result_event.get("is_error"):
        return False  # a real error is already surfaced by returncode/is_error, not this heuristic's job
    return not used_wiki_tool


def run_headless_agent(url, prompt, timeout_seconds=1200, max_attempts=5, retry_delay_seconds=10):
    """Runs one headless `claude -p` invocation against the given HTTP/SSE
    MCP server URL (see WIKI_MCP_BASE_URL / mcp_http_server.py). Returns
    {ok, returncode, stdout, stderr, attempts} -- ok is False on a nonzero
    exit, a timeout, or exhausting all retries on the startup-race/never
    -connected signature above; never raises (a batch failure shouldn't
    crash the whole driver; the caller records it and moves on)."""
    last_proc = None
    for attempt in range(1, max_attempts + 1):
        try:
            proc = _invoke_once(url, prompt, timeout_seconds)
        except subprocess.TimeoutExpired as e:
            return {"ok": False, "returncode": None, "stdout": e.stdout or "", "stderr": f"timed out after {timeout_seconds}s", "attempts": attempt}
        last_proc = proc
        result_event, used_wiki_tool = _parse_stream_json(proc.stdout)
        if proc.returncode == 0 and not _looks_like_mcp_startup_race(result_event, used_wiki_tool):
            return {"ok": True, "returncode": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr, "attempts": attempt}
        if attempt < max_attempts:
            time.sleep(retry_delay_seconds)
    return {"ok": False, "returncode": last_proc.returncode, "stdout": last_proc.stdout, "stderr": last_proc.stderr, "attempts": max_attempts, "mcp_never_connected": True}
