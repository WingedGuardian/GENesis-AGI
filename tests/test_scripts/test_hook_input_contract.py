"""Regression guard for the CC hook-input contract.

Claude Code delivers each hook's payload as JSON on **stdin**, with the tool
arguments nested under ``tool_input`` (e.g. ``{"tool_name": "Bash",
"tool_input": {"command": "..."}}``). An earlier generation of Genesis hooks
read a ``CLAUDE_TOOL_INPUT`` environment variable that held *only* the
tool-input dict. Current Claude Code does not set that variable, so every hook
that relied on it silently fell open — the CRITICAL-path guard, the push/merge
gate, the destructive-``rm`` guard and a dozen others all became no-ops (found
live 2026-07-23; see docs/reference/cc-compatibility.md).

These tests feed each safety-critical guard a REAL-shaped payload on stdin and
assert it still acts (exit 2), plus a static guard that no hook re-introduces a
raw ``CLAUDE_TOOL_INPUT`` env read outside the shared helper. Install-agnostic:
synthetic payloads, subprocess isolation, no network, no live DB.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_SCRIPTS = _REPO / "scripts"


def _payload(tool_name: str, tool_input: dict) -> str:
    """A real CC PreToolUse payload: full envelope, args nested under tool_input."""
    return json.dumps(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": tool_name,
            "tool_input": tool_input,
            "session_id": "test",
            "cwd": str(_REPO),
        }
    )


def _run(script_rel: str, stdin: str, *, env_var: str | None = None):
    """Run a hook script, payload on stdin (or the legacy env var). Returns CompletedProcess.

    CLAUDE_TOOL_INPUT is scrubbed from the child env so a stray value on the
    test host can never mask a stdin-parsing regression.
    """
    import os

    env = dict(os.environ)
    env.pop("CLAUDE_TOOL_INPUT", None)
    piped = stdin
    if env_var is not None:
        env["CLAUDE_TOOL_INPUT"] = env_var
        piped = ""
    return subprocess.run(
        [sys.executable, str(_SCRIPTS / script_rel)],
        input=piped,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


# Install-agnostic triggers: the destructive guard blocks by path breadth (home
# is a shallow path on any runner); the protected-paths guard matches the `~`
# alias, so neither case hardcodes this box's `/home/ubuntu`.
_HOME = str(Path.home())

# (script, tool_name, tool_input, must_block) — the safety-critical exit-2 guards.
_BLOCKING_CASES = [
    ("hooks/git_push_guard.py", "Bash", {"command": "git push origin main"}, True),
    ("hooks/git_push_guard.py", "Bash", {"command": "ls -la"}, False),
    ("hooks/destructive_command_guard.py", "Bash", {"command": f"rm -rf {_HOME}"}, True),
    ("hooks/destructive_command_guard.py", "Bash", {"command": "rm file.txt"}, False),
    (
        "hooks/protected_paths_guard.py",
        "Bash",
        {"command": "rm -rf ~/.claude/projects"},
        True,
    ),
    (
        "hooks/worktree_cwd_guard.py",
        "Bash",
        {"command": "git worktree remove foo --force"},
        True,
    ),
    (
        "review_enforcement_commit.py",
        "Bash",
        {"command": "git commit --no-verify -m x"},
        True,
    ),
    ("pretool_check.py", "Write", {"file_path": "/x/secrets.env", "content": "a"}, True),
    ("pretool_check.py", "Write", {"file_path": "/x/normal.py", "content": "a"}, False),
]


@pytest.mark.parametrize("script,tool,ti,must_block", _BLOCKING_CASES)
def test_guard_acts_on_real_stdin_payload(script, tool, ti, must_block):
    """Each guard must (not) block when fed the real stdin+nested payload."""
    proc = _run(script, _payload(tool, ti))
    blocked = proc.returncode == 2
    assert blocked is must_block, (
        f"{script} returncode={proc.returncode} (want block={must_block}); "
        f"stderr={proc.stderr[:200]!r}"
    )


def test_legacy_env_var_still_honored():
    """Backward-compat: a legacy CLAUDE_TOOL_INPUT (tool-input dict only) still blocks."""
    proc = _run(
        "hooks/git_push_guard.py",
        stdin="",
        env_var=json.dumps({"command": "git push origin main"}),
    )
    assert proc.returncode == 2, f"legacy env path regressed: stderr={proc.stderr[:200]!r}"


def test_empty_input_fails_open():
    """No stdin and no env var → allow (never crash, never spuriously block)."""
    proc = _run("hooks/git_push_guard.py", stdin="")
    assert proc.returncode == 0


# Hooks are ALLOWED to read CLAUDE_TOOL_INPUT only inside the shared helper
# (as a legacy fallback). Anywhere else it is the dead-env-var regression.
_ALLOWED_ENV_READERS = {"hook_input.py"}


_DEAD_ENV_VARS = ("CLAUDE_TOOL_INPUT", "CLAUDE_TOOL_USE_RESULT", "CLAUDE_SESSION_ID")


def test_no_hook_reads_dead_env_var_directly():
    """Static guard: no hook reads the dead payload env vars via os.environ.

    Matches the actual regression pattern (an ``os.environ`` read of the dead
    var), not mere string mentions in prose — a docstring may reference the
    historical contract without re-introducing the bug.
    """
    offenders = []
    for path in list(_SCRIPTS.glob("hooks/*.py")) + list(_SCRIPTS.glob("*.py")):
        if path.name in _ALLOWED_ENV_READERS:
            continue
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            if "environ" in line and any(v in line for v in _DEAD_ENV_VARS):
                offenders.append(f"{path.relative_to(_REPO).as_posix()}: {line.strip()}")
    assert not offenders, (
        "These hooks read a dead payload env var via os.environ instead of "
        f"scripts/hooks/hook_input.read_payload(): {offenders}"
    )


def test_no_settings_json_pipes_dead_env_var():
    """Static guard: no settings JSON wraps a hook in `echo $CLAUDE_TOOL_INPUT |`.

    That wrapper clobbers CC's real stdin with the dead env var — the exact
    landmine that silently disabled the guards. Covers the live settings and the
    install-time template.
    """
    offenders = []
    for rel in (".claude/settings.json", "config/claude-settings.json.template"):
        path = _REPO / rel
        if not path.exists():
            continue
        if "CLAUDE_TOOL_INPUT" in path.read_text(encoding="utf-8", errors="ignore"):
            offenders.append(rel)
    assert not offenders, (
        "These settings files still reference the dead CLAUDE_TOOL_INPUT env var "
        f"(hooks must read the payload from stdin): {offenders}"
    )
