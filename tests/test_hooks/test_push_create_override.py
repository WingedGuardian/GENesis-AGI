"""Tests for the push / PR-create approval override in git_push_guard.py.

Post-#1227 the guard blocked `gh pr create` unconditionally (no override at
all) and matched `git push` / `--no-verify` as bare substrings, so a quoted
*mention* tripped them. These tests cover:
- `gh pr create` / `git push` block without approval, but proceed with a
  trailing `# review-override` comment (the in-session-approval signal).
- Quote-aware matching: a subcommand named inside a string is not a real
  invocation and must not block.
- The override must be a genuine trailing comment, not smuggled inside a
  quoted argument (e.g. a `--body`).

Pure string-parsing paths (no gh/git network), so no mocking needed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_GUARD = Path(__file__).resolve().parents[2] / "scripts" / "hooks" / "git_push_guard.py"


def _run(command: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_TOOL_INPUT"}
    payload = json.dumps(
        {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": command}}
    )
    return subprocess.run(
        [sys.executable, str(_GUARD)],
        input=payload,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )


# ── gh pr create ────────────────────────────────────────────────────


def test_pr_create_blocked_without_override():
    res = _run("gh pr create --title x --body y")
    assert res.returncode == 2
    assert "Creating a PR requires user approval" in res.stderr


def test_pr_create_allowed_with_override():
    res = _run("gh pr create --title x --body-file /tmp/b.md  # review-override")
    assert res.returncode == 0, res.stderr
    assert "override honored" in res.stderr


def test_pr_create_override_inside_body_does_not_count():
    """A token smuggled into a quoted --body must NOT be treated as override."""
    res = _run('gh pr create --title x --body "please # review-override"')
    assert res.returncode == 2
    assert "Creating a PR requires user approval" in res.stderr


def test_pr_create_mention_in_string_not_tripped():
    res = _run('echo "run gh pr create later"')
    assert res.returncode == 0


# ── git push ────────────────────────────────────────────────────────


def test_git_push_blocked_without_override():
    res = _run("git push -u origin feature/x")
    assert res.returncode == 2
    assert "git push requires user approval" in res.stderr


def test_git_push_allowed_with_override():
    res = _run("git push -u origin feature/x  # review-override")
    assert res.returncode == 0, res.stderr
    assert "override honored" in res.stderr


def test_git_push_mention_in_string_not_tripped():
    res = _run('echo "git push origin main"')
    assert res.returncode == 0


def test_git_push_with_credential_helper_idiom_blocked():
    """The repo's own push idiom (git -c ... push) must be caught, not evaded."""
    res = _run("git -c credential.helper='!gh auth git-credential' push -u origin feat/x")
    assert res.returncode == 2
    assert "git push requires user approval" in res.stderr


def test_git_push_with_credential_helper_idiom_override():
    res = _run(
        "git -c credential.helper='!gh auth git-credential' push -u origin feat/x  # review-override"
    )
    assert res.returncode == 0, res.stderr
    assert "override honored" in res.stderr


def test_git_config_with_push_value_not_blocked():
    """`git config ...push...` is a config write, not a push — must not block."""
    res = _run("git config --get remote.origin.push")
    assert res.returncode == 0


# ── git commit --no-verify (quote-aware) ────────────────────────────


def test_no_verify_flag_blocked():
    res = _run('git commit --no-verify -m "wip"')
    assert res.returncode == 2
    assert "no-verify" in res.stderr


def test_no_verify_mentioned_in_message_not_blocked():
    res = _run('git commit -m "document the --no-verify footgun"')
    assert res.returncode == 0, res.stderr
