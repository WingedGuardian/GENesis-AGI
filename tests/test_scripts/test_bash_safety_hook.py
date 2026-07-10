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


# --- Worktree runtime-boot guard (2026-07-03 container-OOM incident) ---

def _run_cwd(command: str, cwd) -> subprocess.CompletedProcess:
    """Like _run but with an explicit cwd, so worktree-cwd detection is
    deterministic regardless of where pytest itself runs."""
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
    env = dict(os.environ)
    env.pop("GENESIS_BASH_ALLOWLIST", None)
    return subprocess.run(
        ["bash", str(HOOK)],
        input=payload, capture_output=True, text=True, env=env, cwd=str(cwd),
    )


@pytest.mark.parametrize("cmd", [
    "PYTHONPATH=/home/u/genesis/.claude/worktrees/foo/src python -m genesis serve --port 5000",
    "cd .claude/worktrees/my-branch && python -m genesis serve",
    "PYTHONPATH=.claude/worktrees/x/src .venv/bin/python -m genesis serve",
])
def test_worktree_serve_blocked(cmd, tmp_path):
    """Booting the full runtime from/against a worktree is blocked (exit 2)."""
    result = _run_cwd(cmd, tmp_path)
    assert result.returncode == 2
    assert "BLOCKED" in result.stderr


@pytest.mark.parametrize("cmd", [
    "systemctl --user restart genesis-server",
    "journalctl --user -u genesis-server -n 50",
    "python -m genesis serve --port 5000",  # no worktree ref, non-worktree cwd
])
def test_non_worktree_serve_paths_allowed(cmd, tmp_path):
    """Server management and plain serve (outside a worktree) pass this guard."""
    assert _run_cwd(cmd, tmp_path).returncode == 0


# --- gh pr merge: PR resolution fails CLOSED (2026-07-10 P1 triage) ---

def _gh_stub(tmp_path: Path, script: str) -> dict[str, str]:
    """Put a fake `gh` first on PATH so no test touches the network."""
    stub = tmp_path / "gh"
    stub.write_text(f"#!/usr/bin/env bash\n{script}\n")
    stub.chmod(0o755)
    return {"PATH": f"{tmp_path}:{os.environ['PATH']}"}


def test_merge_no_arg_unresolvable_blocks(tmp_path):
    """No number in the command AND no open PR for the branch -> exit 2."""
    env = _gh_stub(tmp_path, "exit 1")
    result = _run("gh pr merge --squash --admin", env_extra=env)
    assert result.returncode == 2
    assert "cannot resolve" in result.stderr


def test_merge_no_arg_resolves_branch_pr(tmp_path):
    """No number, but the branch has an open PR -> gates run against it."""
    env = _gh_stub(
        tmp_path,
        'case "$*" in *"--json number"*) echo 42;; '
        '*"--json mergeable"*) echo MERGEABLE;; esac',
    )
    result = _run("gh pr merge --squash", env_extra=env)
    assert result.returncode == 0
    assert "PR #42" in result.stderr


def test_merge_numbered_conflicting_blocks(tmp_path):
    env = _gh_stub(
        tmp_path,
        'case "$*" in *"--json mergeable"*) echo CONFLICTING;; esac',
    )
    result = _run("gh pr merge 123 --squash", env_extra=env)
    assert result.returncode == 2
    assert "merge conflicts" in result.stderr


# --- gh pr merge: PR number after a FLAG must resolve correctly ---
# (2026-07-10 review: an anchored "merge <digits>" match missed
#  `gh pr merge --admin 123` and fell back to the WRONG branch PR.)

@pytest.mark.parametrize("cmd", [
    "gh pr merge --admin 123",
    "gh pr merge 123 --admin",
    "gh pr merge --squash 123 --admin",
    "gh pr merge https://github.com/o/r/pull/123 --squash",
])
def test_merge_number_after_flag_resolves_correctly(tmp_path, cmd):
    """The PR named in the command is checked, regardless of flag order.

    The gh stub returns branch-PR #55; a correct parse must report #123,
    not #55.
    """
    env = _gh_stub(
        tmp_path,
        'case "$*" in *"--json number"*) echo 55;; '
        '*"--json mergeable"*) echo MERGEABLE;; esac',
    )
    result = _run(cmd, env_extra=env)
    assert "PR #123" in result.stderr
    assert "PR #55" not in result.stderr


def test_merge_digits_in_quoted_subject_not_a_pr(tmp_path):
    """Digits inside a quoted --subject must not be taken as the PR."""
    env = _gh_stub(
        tmp_path,
        'case "$*" in *"--json number"*) echo 77;; '
        '*"--json mergeable"*) echo MERGEABLE;; esac',
    )
    # Only digits present are inside the quoted subject -> fall back to
    # the branch PR (#77), never "999".
    result = _run('gh pr merge --subject "merge 999 now"', env_extra=env)
    assert "PR #77" in result.stderr
    assert "999" not in result.stderr


def test_merge_chained_command_digits_ignored(tmp_path):
    """`gh pr merge 123; echo 456` must check PR #123, not #456 (a chained
    command's digits are not this merge's target). 2026-07-10 review."""
    env = _gh_stub(
        tmp_path,
        'case "$*" in *"--json mergeable"*) echo MERGEABLE;; esac',
    )
    result = _run("gh pr merge 123 --admin; echo 456", env_extra=env)
    assert "PR #123" in result.stderr
    assert "456" not in result.stderr
