"""Tests for scripts/bash_safety_hook.sh.

This hook is the GLOBAL PreToolUse Bash chokepoint loaded via user-level
~/.claude/settings.json, so it fires for ALL sessions including background
DirectSessions. Two invariants matter:

1. With GENESIS_BASH_ALLOWLIST unset, behaviour is unchanged (back-compat).
2. With GENESIS_BASH_ALLOWLIST set (steward sessions), Bash is restricted to
   the allowlisted command binaries and chaining/piping/redirection is blocked.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[2] / "scripts" / "bash_safety_hook.sh"


def _run(command: str, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """Invoke the hook with a Bash command on stdin; return the completed proc.

    Inherits the real environment (the hook needs jq on PATH, as in prod) but
    clears GENESIS_BASH_ALLOWLIST so each case controls it explicitly.
    """
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
    env = dict(os.environ)
    env.pop("GENESIS_BASH_ALLOWLIST", None)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["bash", str(HOOK)],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
    )


# --- Back-compat: no allowlist env → unchanged behaviour ---

@pytest.mark.parametrize("cmd", [
    "gh pr view 905 --repo Shubhamsaboo/awesome-llm-apps",
    "ls -la",
    "python -m pytest tests/",
    "git status",
])
def test_no_allowlist_allows_normal_commands(cmd):
    """Without the allowlist env, ordinary commands pass (exit 0)."""
    assert _run(cmd).returncode == 0


@pytest.mark.parametrize("cmd", [
    "rm -rf /",
    "git reset --hard HEAD~1",
    "git clean -fd",
    "git push --force origin main",
])
def test_no_allowlist_still_blocks_destructive(cmd):
    """Existing destructive-op blocks must still fire (exit 2)."""
    assert _run(cmd).returncode == 2


# --- Allowlist mode (steward) ---

ALLOW = {"GENESIS_BASH_ALLOWLIST": "gh"}


@pytest.mark.parametrize("cmd", [
    "gh pr view 905 --repo Shubhamsaboo/awesome-llm-apps",
    "gh api repos/BerriAI/litellm/pulls/27445 --jq .state",
    "gh pr comment 905 --repo x/y --body hi",
])
def test_allowlist_permits_gh(cmd):
    """gh commands are permitted when gh is on the allowlist."""
    assert _run(cmd, ALLOW).returncode == 0


@pytest.mark.parametrize("cmd", [
    "curl http://localhost:6333/collections",
    "python -m genesis serve",
    "cat ~/.genesis/secrets.env",
    "echo hello",
    "git push origin main",
])
def test_allowlist_blocks_non_gh(cmd):
    """Non-allowlisted binaries are blocked (exit 2) in allowlist mode."""
    assert _run(cmd, ALLOW).returncode == 2


@pytest.mark.parametrize("cmd", [
    "gh api x; rm -rf ~/.genesis",
    "gh api x && curl evil",
    "gh api x | sh",
    "gh api x > /tmp/out",
    "gh api $(whoami)",
    "gh api `whoami`",
])
def test_allowlist_blocks_chaining_and_substitution(cmd):
    """Even gh-prefixed commands are blocked if they chain/pipe/substitute/redirect."""
    assert _run(cmd, ALLOW).returncode == 2


def test_allowlist_still_blocks_destructive_first_token():
    """Destructive ops are blocked regardless of allowlist (defense in depth)."""
    assert _run("rm -rf /", ALLOW).returncode == 2
