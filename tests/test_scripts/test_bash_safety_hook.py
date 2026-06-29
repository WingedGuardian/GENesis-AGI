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


# --- Force-push detection: a FLAG token, not a branch-name substring ---

@pytest.mark.parametrize("cmd", [
    "git push -f origin main",
    "git push --force origin main",
    "git push origin main --force",
    "git push --force-with-lease origin main",
    "git push origin HEAD -f",
    "git push -fv origin main",   # bundled short flags (force + verbose)
    "git push -uf origin main",   # bundled (set-upstream + force)
])
def test_force_push_variants_blocked(cmd):
    """Real force pushes (a standalone -f flag or any --force* variant) are
    hard-blocked (exit 2)."""
    assert _run(cmd).returncode == 2


@pytest.mark.parametrize("cmd", [
    "git push origin learning/lc2-honest-skill-funnel",  # '-f' inside 'skill-funnel'
    "git push origin bug-fix",                            # '-f' inside 'bug-fix'
    "git push origin feature/new-flow",                  # '-f' inside 'new-flow'
    "git push origin HEAD",
    "git push origin main",
])
def test_normal_push_with_dash_f_in_branch_not_blocked(cmd):
    """A branch name that merely CONTAINS the literal '-f' must NOT be treated as
    a force push. These clear the hard-block (the soft approval reminder still
    fires on stderr, but exit code is 0)."""
    assert _run(cmd).returncode == 0


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
    "gh api x\ncurl evil",          # newline-chained second command (injection bypass)
    'gh pr comment 1 --body "a\nb"',  # embedded newline in a gh arg
])
def test_allowlist_blocks_chaining_and_substitution(cmd):
    """Even gh-prefixed commands are blocked if they chain/pipe/substitute/redirect."""
    assert _run(cmd, ALLOW).returncode == 2


def test_allowlist_still_blocks_destructive_first_token():
    """Destructive ops are blocked regardless of allowlist (defense in depth)."""
    assert _run("rm -rf /", ALLOW).returncode == 2
