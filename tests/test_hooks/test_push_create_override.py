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


# ── shell-parse hardening: wrappers, quoted flags, per-segment override ──


def test_push_via_sudo_blocked():
    res = _run("sudo git push origin main")
    assert res.returncode == 2
    assert "git push requires user approval" in res.stderr


def test_push_via_env_prefix_blocked():
    res = _run("env FOO=1 git push origin main")
    assert res.returncode == 2


def test_push_via_absolute_path_blocked():
    res = _run("/usr/bin/git push origin main")
    assert res.returncode == 2


def test_quoted_no_verify_flag_blocked():
    """The shell still passes a quoted '--no-verify' to git → must block."""
    res = _run("git commit '--no-verify' -m wip")
    assert res.returncode == 2
    assert "no-verify" in res.stderr


def test_no_verify_bundled_with_operator_blocked():
    """-n glued to a shell operator is still a real flag → must block."""
    res = _run("git commit -m wip -n&&echo done")
    assert res.returncode == 2


def test_no_verify_in_bash_c_script_blocked():
    """The inner command of bash -c is executed → must be seen."""
    res = _run("bash -c 'git commit -n -m wip'")
    assert res.returncode == 2


def test_commit_attached_message_not_false_blocked():
    """git commit -minitial is `-m initial`, not a -n bundle → must not block."""
    res = _run("git commit -minitial")
    assert res.returncode == 0, res.stderr


def test_override_does_not_span_to_next_command():
    """A push override must not authorize a following pr-create (own segment)."""
    res = _run("git push origin feat # review-override\ngh pr create --title x --body y")
    assert res.returncode == 2
    assert "Creating a PR requires user approval" in res.stderr


# ── second-review hardening: wrapper args, substitutions, gh global flag ──


def test_push_via_timeout_positional_blocked():
    """timeout's DURATION positional must not be mistaken for the executable."""
    res = _run("timeout 5 git push origin main")
    assert res.returncode == 2


def test_push_via_sudo_user_flag_blocked():
    res = _run("sudo -u root git push origin main")
    assert res.returncode == 2


def test_push_via_nice_flag_blocked():
    res = _run("nice -n 10 git push origin main")
    assert res.returncode == 2


def test_push_in_command_substitution_blocked():
    res = _run('echo "$(git push origin main)"')
    assert res.returncode == 2


def test_no_verify_via_bash_lc_bundle_blocked():
    """Combined interpreter flags (bash -lc) must still recurse into the script."""
    res = _run("bash -lc 'git commit -n -m wip'")
    assert res.returncode == 2


def test_pr_create_with_global_flag_before_pr_blocked():
    """A gh global flag before `pr` must not evade the create gate."""
    res = _run("gh --repo o/r pr create --title x --body y")
    assert res.returncode == 2
    assert "Creating a PR requires user approval" in res.stderr


def test_commit_message_starting_with_dash_n_not_false_blocked():
    """A -m message value beginning with -n is a message, not a flag."""
    res = _run("git commit -m '-not ready yet'")
    assert res.returncode == 0, res.stderr


def test_commit_pathspec_after_double_dash_not_false_blocked():
    res = _run("git commit -- -n")
    assert res.returncode == 0, res.stderr
