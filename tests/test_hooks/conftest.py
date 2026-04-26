"""Shared fixtures for inline hook tests."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest


def _load_settings() -> dict:
    """Load .claude/settings.json from the repo root."""
    here = Path(__file__).resolve()
    for ancestor in here.parents:
        candidate = ancestor / ".claude" / "settings.json"
        if candidate.exists():
            return json.loads(candidate.read_text())
    raise FileNotFoundError(
        "Could not find .claude/settings.json in any parent directory"
    )


def _find_hook_command(settings: dict, matcher: str) -> str:
    """Extract the command for a PreToolUse hook by matcher name.

    Handles both inline hooks (``bash -c '...'``) and external script paths.
    """
    pre_tool_hooks = settings.get("hooks", {}).get("PreToolUse", [])
    for entry in pre_tool_hooks:
        if entry.get("matcher") == matcher:
            hooks = entry.get("hooks", [])
            for hook in hooks:
                if hook.get("type") == "command":
                    cmd = hook["command"]
                    # Inline hooks start with "bash -c"; external scripts don't.
                    return cmd
    raise ValueError(
        f"No hook command found for matcher '{matcher}' in settings.json"
    )


@pytest.fixture(scope="session")
def settings() -> dict:
    """Parsed .claude/settings.json."""
    return _load_settings()


@pytest.fixture(scope="session")
def bash_hook_command(settings: dict) -> str:
    """The inline bash -c command string for the Bash PreToolUse hook."""
    return _find_hook_command(settings, "Bash")


@pytest.fixture(scope="session")
def rm_rf_hook_command() -> str:
    """Command to run the destructive_command_guard.py script directly.

    The rm-rf guard is a separate Python script (not the inline bash hook).
    This fixture resolves the script path and returns a shell command that
    invokes it via the venv Python, matching how genesis-hook runs it.
    """
    here = Path(__file__).resolve()
    for ancestor in here.parents:
        script = ancestor / "scripts" / "hooks" / "destructive_command_guard.py"
        if script.exists():
            venv_python = ancestor / ".venv" / "bin" / "python"
            python = str(venv_python) if venv_python.exists() else "python3"
            return f"{python} {script}"
    raise FileNotFoundError("Could not find destructive_command_guard.py")


@pytest.fixture(scope="session")
def webfetch_hook_command(settings: dict) -> str:
    """The raw bash -c command string for the WebFetch PreToolUse hook."""
    return _find_hook_command(settings, "WebFetch")


def run_hook(hook_command: str, tool_input: dict) -> subprocess.CompletedProcess:
    """Run an inline hook command with CLAUDE_TOOL_INPUT set.

    Mirrors how Claude Code invokes PreToolUse hooks: it sets the
    CLAUDE_TOOL_INPUT env var to the JSON-serialized tool input, then
    runs the hook command via the shell.

    Args:
        hook_command: The full "bash -c '...'" command from settings.json.
        tool_input: Dict to serialize as JSON into CLAUDE_TOOL_INPUT env var.

    Returns:
        CompletedProcess with returncode, stdout, stderr.
    """
    env = {
        **os.environ,
        "CLAUDE_TOOL_INPUT": json.dumps(tool_input),
    }
    result = subprocess.run(
        hook_command,
        shell=True,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return result
