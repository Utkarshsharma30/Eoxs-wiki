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


def _build_mcp_config(server_module, env):
    config = {
        "mcpServers": {
            "wiki": {
                "command": PYTHON,
                "args": ["-m", server_module],
                "cwd": str(REPO_ROOT),
                "env": env or {},
            }
        }
    }
    fd, path = tempfile.mkstemp(suffix=".json", prefix="wiki_mcp_config_")
    with os.fdopen(fd, "w") as f:
        json.dump(config, f)
    return path


def _invoke_once(server_module, env, prompt, timeout_seconds):
    mcp_config_path = _build_mcp_config(server_module, env)
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


def run_headless_agent(server_module, env, prompt, timeout_seconds=1200, max_attempts=3, retry_delay_seconds=5):
    """Runs one headless `claude -p` invocation against the given stdio MCP
    server module (e.g. 'wiki_ingestion.agent_mcp_server'), with `env`
    passed to that server subprocess. Returns {ok, returncode, stdout,
    stderr, attempts} -- ok is False on a nonzero exit, a timeout, or
    exhausting all retries on the startup-race signature above; never
    raises (a batch failure shouldn't crash the whole driver; the caller
    records it and moves on)."""
    last_proc = None
    for attempt in range(1, max_attempts + 1):
        try:
            proc = _invoke_once(server_module, env, prompt, timeout_seconds)
        except subprocess.TimeoutExpired as e:
            return {"ok": False, "returncode": None, "stdout": e.stdout or "", "stderr": f"timed out after {timeout_seconds}s", "attempts": attempt}
        last_proc = proc
        if proc.returncode == 0 and not _looks_like_mcp_startup_race(proc):
            return {"ok": True, "returncode": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr, "attempts": attempt}
        if attempt < max_attempts:
            time.sleep(retry_delay_seconds)
    return {"ok": False, "returncode": last_proc.returncode, "stdout": last_proc.stdout, "stderr": last_proc.stderr, "attempts": max_attempts}
