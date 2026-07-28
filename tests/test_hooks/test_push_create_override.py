"""Tests for the push / PR-create approval gate in git_push_guard.py.

The push and PR-create gates are session-aware:
- Interactive human session (no ``GENESIS_CC_SESSION``) → a native approve/deny
  dialog (``permissionDecision:ask`` on stdout, exit 0) that only the user can
  satisfy — the agent cannot self-approve.
- Genesis-dispatched session (``GENESIS_CC_SESSION=1``) → hard-denied (exit 2):
  no human to prompt, and real autonomous delivery goes through the scope-gated
  server path (``autonomy/executor``), not the CC Bash tool.

The ``# review-override`` token that once bypassed push/PR-create is retired for
those two gates (the dialog replaces it, so the agent can no longer self-approve);
it still governs the pr-merge review-findings gate (see test_merge_review_gate.py).
All shell-parse bypass-hardening from #1232 is preserved — a push detected through
a wrapper is still caught, now denied in the dispatched context.

Pure string-parsing paths (no gh/git network), so no mocking needed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_GUARD = Path(__file__).resolve().parents[2] / "scripts" / "hooks" / "git_push_guard.py"


def _run(command: str, *, dispatched: bool = False) -> subprocess.CompletedProcess:
    # Explicitly set the session kind — never leak the ambient value.
    env = {
        k: v for k, v in os.environ.items() if k not in ("CLAUDE_TOOL_INPUT", "GENESIS_CC_SESSION")
    }
    if dispatched:
        env["GENESIS_CC_SESSION"] = "1"
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


def _decision(res: subprocess.CompletedProcess) -> str | None:
    """The permissionDecision emitted on stdout, or None."""
    try:
        return json.loads(res.stdout)["hookSpecificOutput"]["permissionDecision"]
    except Exception:
        return None


# ── gh pr create ────────────────────────────────────────────────────


def test_pr_create_not_gated_interactive():
    """gh pr create is NOT gated — opening a PR on already-pushed code is a
    review request, not a code-publish (the push is what's gated). No prompt."""
    res = _run("gh pr create --title x --body y")
    assert res.returncode == 0, res.stderr
    assert _decision(res) is None


def test_pr_create_not_gated_dispatched():
    """Un-gated in every session: a dispatched session cannot push code (that is
    denied), so a PR-create on already-pushed code needs no separate gate."""
    res = _run("gh pr create --title x --body y", dispatched=True)
    assert res.returncode == 0, res.stderr
    assert _decision(res) is None


def test_pr_create_mention_in_string_not_tripped():
    res = _run('echo "run gh pr create later"')
    assert res.returncode == 0
    assert _decision(res) is None


def test_pr_create_with_global_flag_not_gated():
    res = _run("gh --repo o/r pr create --title x --body y")
    assert res.returncode == 0, res.stderr
    assert _decision(res) is None


# ── git push ────────────────────────────────────────────────────────


def test_git_push_interactive_asks():
    res = _run("git push -u origin feature/x")
    assert res.returncode == 0, res.stderr
    assert _decision(res) == "ask"


def test_git_push_dispatched_denied():
    res = _run("git push -u origin feature/x", dispatched=True)
    assert res.returncode == 2
    assert _decision(res) is None
    assert "git push requires user approval" in res.stderr


def test_git_push_mention_in_string_not_tripped():
    res = _run('echo "git push origin main"')
    assert res.returncode == 0
    assert _decision(res) is None


def test_git_config_with_push_value_not_tripped():
    """`git config ...push...` is a config write, not a push — must not ask/deny."""
    res = _run("git config --get remote.origin.push")
    assert res.returncode == 0
    assert _decision(res) is None


# ── force push is destructive → HARD-blocked in EVERY session (never an ask) ──


def test_force_push_flag_blocked_interactive():
    res = _run("git push --force origin main")
    assert res.returncode == 2
    assert _decision(res) is None
    assert "Force push" in res.stderr


def test_force_push_short_flag_blocked_interactive():
    res = _run("git push -f origin main")
    assert res.returncode == 2
    assert _decision(res) is None


def test_force_push_quoted_flag_blocked_interactive():
    """Regression (reviewer BLOCKER): a QUOTED force flag must not slip through
    to a generic approval prompt — argv is quote-stripped, so it is caught and
    hard-blocked, never turned into an approvable ask."""
    res = _run("git push origin '-f'")
    assert res.returncode == 2
    assert _decision(res) is None


def test_force_push_with_lease_blocked_interactive():
    res = _run("git push --force-with-lease origin main")
    assert res.returncode == 2
    assert _decision(res) is None


def test_force_push_bundled_short_flag_blocked():
    res = _run("git push -uf origin main")
    assert res.returncode == 2
    assert _decision(res) is None


def test_force_push_blocked_even_dispatched():
    res = _run("git push --force origin main", dispatched=True)
    assert res.returncode == 2


def test_push_option_glued_not_mistaken_for_force():
    """`-oci.skip` is a push-option value, not a force flag → interactive asks."""
    res = _run("git push -oci.skip origin main")
    assert res.returncode == 0, res.stderr
    assert _decision(res) == "ask"


def test_push_branch_name_with_dash_f_asks_not_force_blocked():
    """A branch name containing '-f' is a positional, not a force flag → the
    guard asks (interactive) rather than force-blocking it."""
    res = _run("git push origin fix/guard-false-positives")
    assert res.returncode == 0, res.stderr
    assert _decision(res) == "ask"


def test_force_push_plus_refspec_blocked():
    """`+<ref>` is git shorthand for --force on that ref → hard-block, not ask."""
    res = _run("git push origin +main")
    assert res.returncode == 2
    assert _decision(res) is None


def test_force_push_plus_src_dst_refspec_blocked():
    res = _run("git push origin +feature:main")
    assert res.returncode == 2
    assert _decision(res) is None


def test_force_push_plus_refspec_blocked_even_dispatched():
    res = _run("git push origin +main", dispatched=True)
    assert res.returncode == 2


def test_push_option_value_with_plus_not_mistaken_for_force():
    """A push-option value starting with '+' is not a force refspec → asks."""
    res = _run("git push -o +ci.custom origin main")
    assert res.returncode == 0, res.stderr
    assert _decision(res) == "ask"


def test_force_push_mirror_blocked():
    """--mirror force-updates all refs and deletes remote refs → hard-block."""
    res = _run("git push --mirror origin")
    assert res.returncode == 2
    assert _decision(res) is None


def test_force_push_mirror_blocked_even_dispatched():
    res = _run("git push --mirror origin", dispatched=True)
    assert res.returncode == 2


# ── compound: multiple PUSHES block (each needs own approval); a push + an
#    un-gated pr-create asks ONCE (for the push), and the pr-create rides along ──


def test_push_and_pr_create_compound_asks_once():
    """push && gh pr create is ONE prompt — for the push. gh pr create is not a
    code-publish (it opens a review request on already-pushed code), so it rides
    on the push's approval instead of demanding its own (user direction)."""
    res = _run("git push origin feat && gh pr create --title x --body y")
    assert res.returncode == 0, res.stderr
    assert _decision(res) == "ask"


def test_two_pushes_compound_blocked():
    """Two real pushes still block — each publishes code and needs its own
    approval, so they cannot share a single prompt."""
    res = _run("git push origin a && git push origin b")
    assert res.returncode == 2
    assert _decision(res) is None
    assert "separate" in res.stderr


def test_two_pr_creates_not_gated():
    res = _run("gh pr create --title a --body b && gh pr create --title c --body d")
    assert res.returncode == 0, res.stderr
    assert _decision(res) is None


def test_single_push_with_nongated_op_still_asks():
    """A single push next to a non-publish command still just asks (gated == 1)."""
    res = _run("git push origin feat && ls -la")
    assert res.returncode == 0, res.stderr
    assert _decision(res) == "ask"


# ── the retired `# review-override` token cannot self-approve push/create ──


def test_push_override_token_is_inert_interactive():
    """The old token no longer bypasses push — interactive still gets the dialog."""
    res = _run("git push -u origin feature/x  # review-override")
    assert res.returncode == 0, res.stderr
    assert _decision(res) == "ask"


def test_push_override_token_cannot_self_approve_dispatched():
    """The key security property: a dispatched agent cannot append the token to
    bypass the deny — only the user, via the interactive dialog, can approve."""
    res = _run("git push -u origin feature/x  # review-override", dispatched=True)
    assert res.returncode == 2
    assert _decision(res) is None


def test_pr_create_not_gated_with_token():
    """gh pr create is un-gated regardless of any trailing token — no prompt."""
    res = _run("gh pr create --title x --body-file /tmp/b.md  # review-override")
    assert res.returncode == 0, res.stderr
    assert _decision(res) is None


# ── shell-parse bypass-hardening preserved (dispatched → still hard-denied) ──


def test_push_via_credential_helper_idiom_detected():
    """The repo's own push idiom (git -c ... push) must be caught, not evaded."""
    res = _run(
        "git -c credential.helper='!gh auth git-credential' push -u origin feat/x",
        dispatched=True,
    )
    assert res.returncode == 2
    assert "git push requires user approval" in res.stderr


def test_push_via_sudo_detected():
    res = _run("sudo git push origin main", dispatched=True)
    assert res.returncode == 2


def test_push_via_env_prefix_detected():
    res = _run("env FOO=1 git push origin main", dispatched=True)
    assert res.returncode == 2


def test_push_via_absolute_path_detected():
    res = _run("/usr/bin/git push origin main", dispatched=True)
    assert res.returncode == 2


def test_push_via_timeout_positional_detected():
    res = _run("timeout 5 git push origin main", dispatched=True)
    assert res.returncode == 2


def test_push_via_sudo_user_flag_detected():
    res = _run("sudo -u root git push origin main", dispatched=True)
    assert res.returncode == 2


def test_push_via_nice_flag_detected():
    res = _run("nice -n 10 git push origin main", dispatched=True)
    assert res.returncode == 2


def test_push_in_command_substitution_detected():
    res = _run('echo "$(git push origin main)"', dispatched=True)
    assert res.returncode == 2


def test_interactive_push_through_wrapper_still_asks():
    """Detection is session-independent; interactive asks instead of denying."""
    res = _run("sudo git push origin main")
    assert res.returncode == 0
    assert _decision(res) == "ask"


# ── git commit --no-verify (session-agnostic hard block; unchanged) ──


def test_no_verify_flag_blocked():
    res = _run('git commit --no-verify -m "wip"')
    assert res.returncode == 2
    assert "no-verify" in res.stderr


def test_no_verify_mentioned_in_message_not_blocked():
    res = _run('git commit -m "document the --no-verify footgun"')
    assert res.returncode == 0, res.stderr


def test_quoted_no_verify_flag_blocked():
    res = _run("git commit '--no-verify' -m wip")
    assert res.returncode == 2
    assert "no-verify" in res.stderr


def test_no_verify_bundled_with_operator_blocked():
    res = _run("git commit -m wip -n&&echo done")
    assert res.returncode == 2


def test_no_verify_in_bash_c_script_blocked():
    res = _run("bash -c 'git commit -n -m wip'")
    assert res.returncode == 2


def test_no_verify_via_bash_lc_bundle_blocked():
    res = _run("bash -lc 'git commit -n -m wip'")
    assert res.returncode == 2


def test_commit_attached_message_not_false_blocked():
    res = _run("git commit -minitial")
    assert res.returncode == 0, res.stderr


def test_commit_message_starting_with_dash_n_not_false_blocked():
    res = _run("git commit -m '-not ready yet'")
    assert res.returncode == 0, res.stderr


def test_commit_pathspec_after_double_dash_not_false_blocked():
    res = _run("git commit -- -n")
    assert res.returncode == 0, res.stderr


# ── compound-command precedence: a hard-block beats the interactive ask ──


def test_push_then_no_verify_hard_blocks_not_asks():
    """`git push && git commit --no-verify` must HARD-BLOCK (the no-verify gate
    takes precedence) — the push must never soften it into an approvable ask."""
    res = _run("git push origin feat && git commit --no-verify -m wip")
    assert res.returncode == 2
    assert _decision(res) is None
    assert "no-verify" in res.stderr
