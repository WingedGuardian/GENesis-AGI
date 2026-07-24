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
    raise FileNotFoundError("Could not find .claude/settings.json in any parent directory")


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
    raise ValueError(f"No hook command found for matcher '{matcher}' in settings.json")


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


def run_hook(
    hook_command: str, tool_input: dict, *, tool_name: str = "Bash"
) -> subprocess.CompletedProcess:
    """Run an inline hook command with the real CC payload on stdin.

    Mirrors how current Claude Code invokes PreToolUse hooks: the full payload
    (``{"tool_name": ..., "tool_input": {...}}``) is delivered as JSON on
    **stdin**, NOT via a ``CLAUDE_TOOL_INPUT`` env var (which CC no longer sets).
    The legacy env var is scrubbed so a stray value can't mask a regression.

    Args:
        hook_command: The full "bash -c '...'" command from settings.json.
        tool_input: The tool-input dict (nested under ``tool_input`` in the payload).
        tool_name: The tool name for the payload envelope (default "Bash").

    Returns:
        CompletedProcess with returncode, stdout, stderr.
    """
    payload = json.dumps(
        {"hook_event_name": "PreToolUse", "tool_name": tool_name, "tool_input": tool_input}
    )
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_TOOL_INPUT"}
    result = subprocess.run(
        hook_command,
        shell=True,
        input=payload,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return result
