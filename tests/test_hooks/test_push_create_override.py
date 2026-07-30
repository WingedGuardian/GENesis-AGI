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

Mostly pure string-parsing paths, so no mocking needed. The gated pr-create cases
resolve their branch against the real remote via ``git ls-remote``; they use a
bogus branch (or tolerate a network failure) so the decision is "gate" either way,
keeping the assertions deterministic regardless of connectivity.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest  # noqa: F401  (used by fixtures/tests appended below)

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


# gh pr create publishes code ONLY in the implicit form (no --head), where gh may
# push the current branch if it isn't fully on the remote. Per `gh pr create
# --help`, "Use --head to explicitly skip any forking or pushing behavior" — so
# ANY explicit --head cannot publish and is un-gated. The un-gated path emits an
# explicit `allow` so CC's own permission prompt for this non-allow-listed command
# does not fire (a bare exit-0 would leave it to prompt). The implicit
# push-or-not decision is git-state dependent, so it's covered deterministically
# by the _pr_create_would_publish unit tests in test_merge_review_gate.py; here we
# assert the deterministic explicit-head behavior (allow, no network).
_HEAD = "zzz-some-branch-xyz"


def test_pr_create_explicit_head_allowed_interactive():
    res = _run(f"gh pr create --head {_HEAD} --title x --body y")
    assert res.returncode == 0, res.stderr
    assert _decision(res) == "allow"  # gh skips push with --head → no prompt


def test_pr_create_explicit_head_allowed_dispatched():
    res = _run(f"gh pr create --head {_HEAD} --title x --body y", dispatched=True)
    assert res.returncode == 0, res.stderr
    assert _decision(res) == "allow"


def test_pr_create_cross_fork_head_allowed():
    """An owner:branch cross-fork head also can't push around the gate (gh can't
    push to a fork we don't control), so it is un-gated too."""
    res = _run("gh pr create --head someone:feature --title x --body y")
    assert res.returncode == 0, res.stderr
    assert _decision(res) == "allow"


def test_pr_create_mention_in_string_not_tripped():
    res = _run('echo "run gh pr create later"')
    assert res.returncode == 0
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


def test_pr_create_token_irrelevant_to_gate():
    """The retired `# review-override` token never factored into pr-create gating
    (it governs the pr-merge findings gate only). An explicit-head create is
    allowed on its own merits — the trailing token neither helps nor changes it."""
    res = _run(f"gh pr create --head {_HEAD} --title x --body-file /tmp/b.md  # review-override")
    assert res.returncode == 0, res.stderr
    assert _decision(res) == "allow"


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


# ── First-push-only gate (re-push to an already-published branch is un-gated) ──
# The load-bearing new behavior gets a REAL local bare remote so the ALLOW path is
# deterministic in CI (not dependent on the network / real origin). Mirrors
# git_push_guard.py::_push_is_republish: a branch already on the remote was
# approved on its first push, so a re-push must not re-prompt; a genuinely-first
# push (branch absent) still asks; dispatched still hard-denies; force still blocks;
# push-to-main / cross-name refspec still ask.


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _run_at(command: str, cwd: str, *, dispatched: bool = False) -> subprocess.CompletedProcess:
    """Run the hook with an explicit payload cwd + matching process cwd (a real repo)."""
    env = {
        k: v for k, v in os.environ.items() if k not in ("CLAUDE_TOOL_INPUT", "GENESIS_CC_SESSION")
    }
    if dispatched:
        env["GENESIS_CC_SESSION"] = "1"
    payload = json.dumps(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": command},
            "cwd": cwd,
        }
    )
    return subprocess.run(
        [sys.executable, str(_GUARD)],
        input=payload,
        env=env,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=25,
    )


@pytest.fixture()
def published_feat(tmp_path):
    """A clone whose branch ``feat/x`` is ALREADY pushed to a local bare remote.

    Returns the clone dir; the checked-out branch is ``feat/x`` (published) with
    ``main`` also on the remote. No network — purely local git.
    """
    remote = tmp_path / "remote.git"
    clone = tmp_path / "clone"
    _git(tmp_path, "init", "--bare", str(remote))
    _git(tmp_path, "clone", str(remote), str(clone))
    _git(clone, "config", "user.email", "t@t.local")
    _git(clone, "config", "user.name", "probe")
    _git(clone, "checkout", "-b", "main")
    (clone / "r.txt").write_text("root\n")
    _git(clone, "add", "r.txt")
    _git(clone, "commit", "-m", "root")
    _git(clone, "push", "-u", "origin", "main")
    _git(clone, "checkout", "-b", "feat/x")
    (clone / "a.txt").write_text("hi\n")
    _git(clone, "add", "a.txt")
    _git(clone, "commit", "-m", "feat")
    _git(clone, "push", "-u", "origin", "feat/x")
    return str(clone)


def test_republish_bare_push_allows(published_feat):
    """On feat/x (already on the remote), a bare `git push` re-push → allow, no prompt."""
    res = _run_at("git push", published_feat)
    assert res.returncode == 0, res.stderr
    assert _decision(res) == "allow"


def test_republish_explicit_push_allows(published_feat):
    """`git push origin feat/x` for an already-published branch → allow."""
    res = _run_at("git push origin feat/x", published_feat)
    assert res.returncode == 0, res.stderr
    assert _decision(res) == "allow"


def test_first_push_new_branch_asks(published_feat):
    """A genuinely-first push of a NOT-yet-published branch still asks."""
    _git(published_feat, "checkout", "-b", "feat/y")
    (Path(published_feat) / "b.txt").write_text("y\n")
    _git(published_feat, "add", "b.txt")
    _git(published_feat, "commit", "-m", "y")
    res = _run_at("git push -u origin feat/y", published_feat)
    assert res.returncode == 0, res.stderr
    assert _decision(res) == "ask"


def test_dispatched_republish_still_denied(published_feat):
    """The human-not-agent boundary holds: a dispatched re-push is hard-denied,
    never silently allowed."""
    res = _run_at("git push", published_feat, dispatched=True)
    assert res.returncode == 2
    assert _decision(res) is None


def test_republish_to_main_refspec_asks(published_feat):
    """`git push origin feat/x:main` (dst=main) must still ask — a push to the
    default branch never goes silent even though main is on the remote."""
    res = _run_at("git push origin feat/x:main", published_feat)
    assert res.returncode == 0, res.stderr
    assert _decision(res) == "ask"


def test_cross_name_refspec_asks(published_feat):
    """`git push origin feat/x:other` (dst != current branch) falls through to ask,
    even if `other` exists on the remote."""
    _git(published_feat, "branch", "other")
    _git(published_feat, "push", "origin", "other")
    res = _run_at("git push origin feat/x:other", published_feat)
    assert res.returncode == 0, res.stderr
    assert _decision(res) == "ask"


def test_force_republish_still_blocks(published_feat):
    """A force re-push of an already-published branch is still HARD-blocked (the
    force arm runs before the republish check)."""
    res = _run_at("git push --force origin feat/x", published_feat)
    assert res.returncode == 2
    assert _decision(res) is None


# ── REGRESSION (reviewer BLOCKER): the -u/--set-upstream parser must not collapse
# the target branch to the current one and silently allow a genuine first push,
# a push to main, a remote-branch delete, or a branch-broadening push. ──


def test_set_upstream_new_branch_still_asks(published_feat):
    """On the published `feat/x`, `git push -u origin feat/z` (a NOT-yet-pushed
    branch) must ASK. The old naive parser ate the `origin` token and made
    `branch == cur` a tautology, silently allowing this genuine first push."""
    _git(published_feat, "branch", "feat/z")  # new local branch, unpublished; stay on feat/x
    res = _run_at("git push -u origin feat/z", published_feat)
    assert res.returncode == 0, res.stderr
    assert _decision(res) == "ask"


def test_set_upstream_main_still_asks(published_feat):
    """`git push -u origin main` while on a published feature branch must ASK — a
    push to the default branch must never go silent."""
    res = _run_at("git push -u origin main", published_feat)
    assert res.returncode == 0, res.stderr
    assert _decision(res) == "ask"


def test_delete_remote_branch_asks(published_feat):
    """Deleting the current branch on the remote (`git push origin :feat/x`) is a
    remote mutation, not a re-push → must ASK."""
    res = _run_at("git push origin :feat/x", published_feat)
    assert res.returncode == 0, res.stderr
    assert _decision(res) == "ask"


def test_push_all_branches_asks(published_feat):
    """`git push --all origin` publishes EVERY local branch — never a plain
    current-branch re-push → must ASK."""
    res = _run_at("git push --all origin", published_feat)
    assert res.returncode == 0, res.stderr
    assert _decision(res) == "ask"


def test_push_delete_flag_asks(published_feat):
    """`git push --delete origin feat/x` deletes a remote branch → must ASK."""
    res = _run_at("git push --delete origin feat/x", published_feat)
    assert res.returncode == 0, res.stderr
    assert _decision(res) == "ask"


# ── REGRESSION (round-2 review): the allowlist predicate must reject bundled short
# delete flags, --stdin, and config-broadened bare pushes — each a silent-allow
# evasion of the first-push-only skip. ──


def test_bundled_short_delete_asks(published_feat):
    """`git push -ud origin feat/x` (= --set-upstream --delete) DELETES the remote
    branch. A bundled short `d` must be caught by the allowlist's letter scan (like
    `-uf` is caught as force) → ASK, not a silent destructive delete."""
    res = _run_at("git push -ud origin feat/x", published_feat)
    assert res.returncode == 0, res.stderr
    assert _decision(res) == "ask"


def test_stdin_flag_asks(published_feat):
    """`git push --stdin origin` reads refspecs from stdin (can target main / delete
    / multiple refs) — not a plain current-branch update → ASK."""
    res = _run_at("git push --stdin origin", published_feat)
    assert res.returncode == 0, res.stderr
    assert _decision(res) == "ask"


def test_config_push_refspec_asks(published_feat):
    """A configured `remote.origin.push = HEAD:main` redirects a bare `git push
    origin` to `main`. The effective-config check must catch it → ASK (a push to
    the default branch must never go silent)."""
    _git(published_feat, "config", "remote.origin.push", "HEAD:main")
    res = _run_at("git push origin", published_feat)
    assert res.returncode == 0, res.stderr
    assert _decision(res) == "ask"


def test_push_default_matching_asks(published_feat):
    """`push.default=matching` broadens a bare push to every same-named branch →
    not a plain current-branch update → ASK."""
    _git(published_feat, "config", "push.default", "matching")
    res = _run_at("git push origin", published_feat)
    assert res.returncode == 0, res.stderr
    assert _decision(res) == "ask"


def test_safe_flags_still_allow(published_feat):
    """Ref-neutral flags on an explicit current-branch re-push still ALLOW — the
    allowlist must not over-prompt the normal `-o ci.skip` / `-uv` forms."""
    res = _run_at("git push -uv -o ci.skip origin feat/x", published_feat)
    assert res.returncode == 0, res.stderr
    assert _decision(res) == "allow"


def test_push_default_upstream_asks(published_feat):
    """REGRESSION (round-3 BLOCKER): with `push.default=upstream` and feat/x tracking
    origin/main, a bare `git push` publishes feat/x's HEAD to `main`. The config
    check must catch it → ASK (never a silent push to main)."""
    _git(published_feat, "branch", "--set-upstream-to=origin/main", "feat/x")
    _git(published_feat, "config", "push.default", "upstream")
    res = _run_at("git push", published_feat)
    assert res.returncode == 0, res.stderr
    assert _decision(res) == "ask"


def test_push_default_tracking_asks(published_feat):
    """`push.default=tracking` (deprecated alias of upstream) → same redirect risk → ASK."""
    _git(published_feat, "branch", "--set-upstream-to=origin/main", "feat/x")
    _git(published_feat, "config", "push.default", "tracking")
    res = _run_at("git push", published_feat)
    assert res.returncode == 0, res.stderr
    assert _decision(res) == "ask"


def test_push_remote_triangular_asks(published_feat, tmp_path):
    """REGRESSION (round-4 BLOCKER): a triangular fork workflow — pull from origin,
    push to a `fork` remote via `branch.<cur>.pushRemote`. feat/x is on origin but
    NOT on fork, so a bare `git push` is a genuine FIRST push to fork → must ASK.
    The republish check must target the ACTUAL push remote (fork), not origin."""
    fork = tmp_path / "fork.git"
    _git(str(tmp_path), "init", "--bare", str(fork))
    _git(published_feat, "remote", "add", "fork", str(fork))
    _git(published_feat, "config", "branch.feat/x.pushRemote", "fork")
    res = _run_at("git push", published_feat)
    assert res.returncode == 0, res.stderr
    assert _decision(res) == "ask"


def test_push_default_remote_triangular_asks(published_feat, tmp_path):
    """Same via `remote.pushDefault=fork` — bare push goes to fork (unpublished) → ASK."""
    fork = tmp_path / "fork2.git"
    _git(str(tmp_path), "init", "--bare", str(fork))
    _git(published_feat, "remote", "add", "fork2", str(fork))
    _git(published_feat, "config", "remote.pushDefault", "fork2")
    res = _run_at("git push", published_feat)
    assert res.returncode == 0, res.stderr
    assert _decision(res) == "ask"
