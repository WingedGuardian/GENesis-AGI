"""Tests for the review findings merge gate in git_push_guard.py.

The gate blocks `gh pr merge` when automated review comments contain
unresolved ERROR/[P1]/HARD BLOCK findings. It fail-opens on API errors
or missing comments.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _hermetic_pr_files(monkeypatch):
    """Hermetic default for the hook-surface override gate's changed-files read:
    without this, force-path tests hit a LIVE `gh api pulls/N/files` call —
    green locally (gh authenticated; PR "1" of the cwd repo answers) and red in
    CI (call fails -> fail-closed block). Tests override per-case."""
    monkeypatch.setenv(
        "_TEST_GH_PR_FILES", '{"filename": "src/benign.py", "previous_filename": null}'
    )

# Resolve the hook script path
_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "hooks"
_GUARD = _SCRIPTS / "git_push_guard.py"

sys.path.insert(0, str(_SCRIPTS))
from shell_parse import (  # noqa: E402
    analyze,
    gh_pr_subcommand,
    git_subcommand,
    has_trailing_override,
)


def _push_seg(command: str):
    """The git-push Segment parsed from a command string (for argv-based helpers)."""
    return next(
        s for s in analyze(command) if s.exe == "git" and git_subcommand(s.argv) == "push"
    )


def _run_guard(
    command: str, *, mock_gh_output: str = "", mock_gh_rc: int = 0
) -> subprocess.CompletedProcess:
    """Run git_push_guard.py with a mock gh api response.

    We patch subprocess.run inside the hook to intercept gh api calls
    while still letting other subprocess calls (like git branch) work.
    """
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_TOOL_INPUT"}
    # Inject mock response via env var — the test wrapper reads it
    env["_TEST_GH_API_OUTPUT"] = mock_gh_output
    env["_TEST_GH_API_RC"] = str(mock_gh_rc)

    # Deliver the command via the real contract: full payload on stdin, nested
    # under tool_input.
    payload = json.dumps(
        {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": command}}
    )
    result = subprocess.run(
        [sys.executable, str(_GUARD)],
        input=payload,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    return result


# ── Import the module directly for unit testing ──────────────────────


@pytest.fixture(scope="module")
def guard_module():
    """Import git_push_guard as a module for direct function testing."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("git_push_guard", _GUARD)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# (The required-CI-workflow seam pin lives in tests/test_hooks/conftest.py —
# one shared autouse fixture, not a per-file replica.)


def _rc(code: int) -> subprocess.CompletedProcess:
    """A fake CompletedProcess carrying just a returncode."""
    return subprocess.CompletedProcess(args=[], returncode=code)


def _proc(code: int, out: str = "") -> subprocess.CompletedProcess:
    """A fake CompletedProcess carrying a returncode and stdout."""
    return subprocess.CompletedProcess(args=[], returncode=code, stdout=out)


def _config_run(values: dict, default: tuple = (1, "")):
    """A ``subprocess.run`` side_effect mapping a git-config KEY (the last argv
    token) to ``(rc, stdout)``. Unlisted keys resolve to ``default`` = ``(1, "")``
    (git's "unset" signal). Order/count-independent — robust to how many config
    reads _push_config_is_simple performs."""

    def run(argv, **kwargs):
        key = argv[-1]
        rc, out = values.get(key, default)
        return _proc(rc, out)

    return run


class TestPrCreateHeadRaw:
    """_pr_create_head_raw returns the RAW --head / -H / --head= value (owner:
    prefix intact), or None. Presence of ANY --head means gh skips push/fork."""

    def test_none_when_no_head(self, guard_module):
        assert guard_module._pr_create_head_raw(["gh", "pr", "create", "--title", "x"]) is None

    def test_head_space_value(self, guard_module):
        assert guard_module._pr_create_head_raw(["gh", "pr", "create", "--head", "feat"]) == "feat"

    def test_head_short_flag(self, guard_module):
        assert guard_module._pr_create_head_raw(["gh", "pr", "create", "-H", "feat"]) == "feat"

    def test_head_equals(self, guard_module):
        assert guard_module._pr_create_head_raw(["gh", "pr", "create", "--head=feat"]) == "feat"

    def test_head_owner_kept_raw(self, guard_module):
        got = guard_module._pr_create_head_raw(["gh", "pr", "create", "--head", "someone:feat"])
        assert got == "someone:feat"

    def test_last_value_wins(self, guard_module):
        # gh's pflag applies last-value-wins for a repeated string flag, so the
        # parser must return the LAST occurrence — not the first.
        assert (
            guard_module._pr_create_head_raw(["gh", "pr", "create", "--head", "a", "--head=b"])
            == "b"
        )
        assert (
            guard_module._pr_create_head_raw(["gh", "pr", "create", "--head", "real", "--head="])
            == ""
        )


class TestPrCreateWouldPublish:
    """Only a create with NO --head can publish (gh may push the current branch
    when it isn't fully on the remote). Any explicit --head makes gh skip
    push/fork → un-gate, no git touched. The implicit case is verified against the
    ACTUAL remote via git ls-remote (not the stale-prone local tracking ref).
    subprocess mocked for determinism. Implicit call order: rev-parse HEAD,
    ls-remote --heads origin <current>, merge-base --is-ancestor HEAD <remote_sha>."""

    def test_explicit_head_never_publishes(self, guard_module):
        # gh --help: "Use --head to explicitly skip any forking or pushing
        # behavior." So ANY explicit head un-gates WITHOUT touching git — incl. a
        # cross-fork owner:branch head (gh can't push to a fork we don't control).
        for argv in (
            ["gh", "pr", "create", "--head", "feat"],
            ["gh", "pr", "create", "-H", "feat"],
            ["gh", "pr", "create", "--head=feat"],
            ["gh", "pr", "create", "--head", "alice:feat"],
        ):
            with patch.object(guard_module.subprocess, "run", side_effect=AssertionError) as run:
                assert guard_module._pr_create_would_publish(argv) is False
            assert run.call_count == 0  # short-circuits before any git call

    def test_untrusted_head_falls_through_to_gate(self, guard_module):
        # Regression (both reviewers): a head that gh would treat as EMPTY, or one
        # we can't verify statically, must NOT un-gate — it falls through to the
        # implicit current-branch push path. Covered:
        #   - literal empty (`--head=` / `--head ""`) → gh's HeadBranch == "";
        #   - repeated flag, LAST value empty → gh's last-wins → "";
        #   - shell-expansion value (`$BRANCH` / `$(...)` / backtick) → may resolve
        #     empty at runtime, unseeable here.
        # With the current branch unknown, each falls through and GATES (not un-gate).
        for argv in (
            ["gh", "pr", "create", "--head="],
            ["gh", "pr", "create", "--head", ""],
            ["gh", "pr", "create", "--head", "real", "--head="],  # last-value-wins
            ["gh", "pr", "create", "--head", "$BRANCH"],  # shell variable
            ["gh", "pr", "create", "--head", "$(git rev-parse --abbrev-ref HEAD)"],
            ["gh", "pr", "create", "--head", "`echo x`"],  # backtick substitution
        ):
            with patch.object(guard_module, "_current_branch", return_value=None):
                assert guard_module._pr_create_would_publish(argv) is True, argv

    def test_no_branch_gates(self, guard_module):
        with patch.object(guard_module, "_current_branch", return_value=None):
            assert guard_module._pr_create_would_publish(["gh", "pr", "create"]) is True

    def test_empty_head_sha_gates(self, guard_module):
        # rev-parse HEAD empty (unborn / odd state) → can't resolve → gate.
        with (
            patch.object(guard_module, "_current_branch", return_value="feat"),
            patch.object(guard_module.subprocess, "run", side_effect=[_proc(0, "")]),
        ):
            assert guard_module._pr_create_would_publish(["gh", "pr", "create"]) is True

    def test_current_branch_absent_on_remote_gates(self, guard_module):
        # ls-remote returns NO matching ref → current branch not pushed → gate.
        with (
            patch.object(guard_module, "_current_branch", return_value="feat"),
            patch.object(
                guard_module.subprocess, "run", side_effect=[_proc(0, "H1"), _proc(0, "")]
            ),
        ):
            assert guard_module._pr_create_would_publish(["gh", "pr", "create"]) is True

    def test_stale_local_ref_no_longer_ungates(self, guard_module):
        # Regression: after a remote branch is deleted (squash-merge auto-delete),
        # the LOCAL tracking ref lingers but ls-remote returns empty → must GATE,
        # NOT trust the stale ref. This is the fail-open the local-ref check had.
        with (
            patch.object(guard_module, "_current_branch", return_value="feat"),
            patch.object(
                guard_module.subprocess, "run", side_effect=[_proc(0, "H1"), _proc(0, "")]
            ) as run,
        ):
            assert guard_module._pr_create_would_publish(["gh", "pr", "create"]) is True
        # Proves it went to the network (ls-remote), not a local remote-tracking ref.
        assert any("ls-remote" in " ".join(c.args[0]) for c in run.call_args_list)

    def test_remote_unreachable_gates(self, guard_module):
        # ls-remote fails (rc!=0: no network / auth) → cannot verify → gate.
        with (
            patch.object(guard_module, "_current_branch", return_value="feat"),
            patch.object(
                guard_module.subprocess, "run", side_effect=[_proc(0, "H1"), _proc(2, "")]
            ),
        ):
            assert guard_module._pr_create_would_publish(["gh", "pr", "create"]) is True

    def test_namespaced_ref_mismatch_gates(self, guard_module):
        # ls-remote pattern-matches tail components: querying short name "feat"
        # can return `refs/heads/ws8/feat` (a DIFFERENT branch). That line's ref
        # path != refs/heads/feat, so it must NOT be accepted → gate (no under-block).
        with (
            patch.object(guard_module, "_current_branch", return_value="feat"),
            patch.object(
                guard_module.subprocess,
                "run",
                side_effect=[_proc(0, "H1"), _proc(0, "aaa111\trefs/heads/ws8/feat")],
            ),
        ):
            assert guard_module._pr_create_would_publish(["gh", "pr", "create"]) is True

    def test_exact_ref_among_multiple_lines_used(self, guard_module):
        # Several lines returned; only the exact refs/heads/<branch> line is used.
        with (
            patch.object(guard_module, "_current_branch", return_value="feat"),
            patch.object(
                guard_module.subprocess,
                "run",
                side_effect=[
                    _proc(0, "bbb222\n"),
                    _proc(0, "aaa111\trefs/heads/ws8/feat\nbbb222\trefs/heads/feat"),
                ],
            ),
        ):
            # HEAD == the EXACT branch's remote tip (bbb222), not aaa111 → ungate.
            assert guard_module._pr_create_would_publish(["gh", "pr", "create"]) is False

    def test_head_is_remote_tip_ungated(self, guard_module):
        # current branch tip (HEAD) == remote tip → nothing to push.
        with (
            patch.object(guard_module, "_current_branch", return_value="feat"),
            patch.object(
                guard_module.subprocess,
                "run",
                side_effect=[_proc(0, "abc123\n"), _proc(0, "abc123\trefs/heads/feat")],
            ),
        ):
            assert guard_module._pr_create_would_publish(["gh", "pr", "create"]) is False

    def test_head_contained_but_not_tip_ungated(self, guard_module):
        # HEAD differs from remote tip but is an ancestor of it (rc=0) → nothing to push.
        with (
            patch.object(guard_module, "_current_branch", return_value="feat"),
            patch.object(
                guard_module.subprocess,
                "run",
                side_effect=[_proc(0, "abc123"), _proc(0, "def456\trefs/heads/feat"), _rc(0)],
            ),
        ):
            assert guard_module._pr_create_would_publish(["gh", "pr", "create"]) is False

    def test_head_ahead_of_remote_gates(self, guard_module):
        # HEAD differs and is NOT an ancestor (rc=1) → unpushed commits → gate.
        with (
            patch.object(guard_module, "_current_branch", return_value="feat"),
            patch.object(
                guard_module.subprocess,
                "run",
                side_effect=[_proc(0, "abc999"), _proc(0, "def456\trefs/heads/feat"), _rc(1)],
            ),
        ):
            assert guard_module._pr_create_would_publish(["gh", "pr", "create"]) is True

    def test_subprocess_error_fails_safe(self, guard_module):
        with (
            patch.object(guard_module, "_current_branch", return_value="feat"),
            patch.object(guard_module.subprocess, "run", side_effect=OSError("boom")),
        ):
            assert guard_module._pr_create_would_publish(["gh", "pr", "create"]) is True

    def test_timeout_fails_safe(self, guard_module):
        with (
            patch.object(guard_module, "_current_branch", return_value="feat"),
            patch.object(
                guard_module.subprocess,
                "run",
                side_effect=subprocess.TimeoutExpired(cmd="git", timeout=10),
            ),
        ):
            assert guard_module._pr_create_would_publish(["gh", "pr", "create"]) is True


# ── _remote_branch_sha / _push_is_republish (first-push gate) ────────


class TestRemoteBranchSha:
    """_remote_branch_sha queries the LIVE remote (git ls-remote) and returns the
    tip sha for EXACTLY refs/heads/<branch>, else None (fail-safe). Shared by the
    pr-create gate and the first-push republish gate."""

    def test_exact_ref_returns_sha(self, guard_module):
        with patch.object(
            guard_module.subprocess, "run", return_value=_proc(0, "abc123\trefs/heads/feat")
        ):
            assert guard_module._remote_branch_sha("origin", "feat") == "abc123"

    def test_namespaced_ref_not_matched(self, guard_module):
        # querying "feat" can tail-match refs/heads/ws8/feat — must NOT accept it.
        with patch.object(
            guard_module.subprocess, "run", return_value=_proc(0, "aaa111\trefs/heads/ws8/feat")
        ):
            assert guard_module._remote_branch_sha("origin", "feat") is None

    def test_exact_line_among_many_used(self, guard_module):
        with patch.object(
            guard_module.subprocess,
            "run",
            return_value=_proc(0, "aaa\trefs/heads/ws8/feat\nbbb\trefs/heads/feat"),
        ):
            assert guard_module._remote_branch_sha("origin", "feat") == "bbb"

    def test_absent_returns_none(self, guard_module):
        with patch.object(guard_module.subprocess, "run", return_value=_proc(0, "")):
            assert guard_module._remote_branch_sha("origin", "feat") is None

    def test_rc_nonzero_returns_none(self, guard_module):
        with patch.object(guard_module.subprocess, "run", return_value=_proc(2, "")):
            assert guard_module._remote_branch_sha("origin", "feat") is None

    def test_timeout_returns_none(self, guard_module):
        with patch.object(
            guard_module.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(cmd="git", timeout=10),
        ):
            assert guard_module._remote_branch_sha("origin", "feat") is None

    def test_oserror_returns_none(self, guard_module):
        with patch.object(guard_module.subprocess, "run", side_effect=OSError("boom")):
            assert guard_module._remote_branch_sha("origin", "feat") is None

    def test_cwd_passed_as_dash_C(self, guard_module):
        with patch.object(guard_module.subprocess, "run", return_value=_proc(0, "")) as run:
            guard_module._remote_branch_sha("origin", "feat", cwd="/wt")
        argv = run.call_args_list[0].args[0]
        assert argv[:3] == ["git", "-C", "/wt"]
        assert "ls-remote" in argv

    def test_no_cwd_omits_dash_C(self, guard_module):
        with patch.object(guard_module.subprocess, "run", return_value=_proc(0, "")) as run:
            guard_module._remote_branch_sha("origin", "feat")
        argv = run.call_args_list[0].args[0]
        assert argv[0] == "git" and argv[1] == "ls-remote"


class TestPushIsRepublish:
    """_push_is_republish: True iff the branch is already on the remote (published
    and approved on its first push). Fail-safe to False (→ prompt) on any
    uncertainty — an unresolved remote/branch never triggers a network call."""

    def test_present_is_republish(self, guard_module):
        with patch.object(guard_module, "_remote_branch_sha", return_value="abc123"):
            assert guard_module._push_is_republish("origin", "feat") is True

    def test_absent_is_not_republish(self, guard_module):
        with patch.object(guard_module, "_remote_branch_sha", return_value=None):
            assert guard_module._push_is_republish("origin", "feat") is False

    def test_none_remote_short_circuits(self, guard_module):
        # unresolved remote → never republish; must NOT touch the remote.
        with patch.object(guard_module, "_remote_branch_sha", side_effect=AssertionError):
            assert guard_module._push_is_republish(None, "feat") is False

    def test_none_branch_short_circuits(self, guard_module):
        with patch.object(guard_module, "_remote_branch_sha", side_effect=AssertionError):
            assert guard_module._push_is_republish("origin", None) is False

    def test_cwd_forwarded(self, guard_module):
        with patch.object(guard_module, "_remote_branch_sha", return_value="x") as srch:
            guard_module._push_is_republish("origin", "feat", cwd="/wt")
        assert srch.call_args_list[0].kwargs.get("cwd") == "/wt"


class TestPushPositionals:
    """_push_positionals extracts [remote, refspec, ...] from a quote-stripped push
    argv, correctly skipping no-value flags (-u/--set-upstream — which do NOT eat
    the next token) and value flags (-o <v>). Empty for a non-push / bare push."""

    def test_bare_push(self, guard_module):
        assert guard_module._push_positionals(["git", "push"]) == []

    def test_remote_only(self, guard_module):
        assert guard_module._push_positionals(["git", "push", "origin"]) == ["origin"]

    def test_remote_and_branch(self, guard_module):
        assert guard_module._push_positionals(["git", "push", "origin", "feat"]) == ["origin", "feat"]

    def test_set_upstream_does_not_eat_remote(self, guard_module):
        # Regression for the naive-split bug: -u / --set-upstream take NO separate
        # token, so 'origin' must remain a positional (not be consumed as a value).
        assert guard_module._push_positionals(["git", "push", "-u", "origin", "feat"]) == [
            "origin",
            "feat",
        ]
        assert guard_module._push_positionals(
            ["git", "push", "--set-upstream", "origin", "feat"]
        ) == ["origin", "feat"]

    def test_value_flag_skips_its_value(self, guard_module):
        assert guard_module._push_positionals(
            ["git", "push", "-o", "ci.skip", "origin", "feat"]
        ) == ["origin", "feat"]

    def test_global_dash_c_skipped(self, guard_module):
        assert guard_module._push_positionals(
            ["git", "-C", "/wt", "push", "origin", "feat"]
        ) == ["origin", "feat"]

    def test_plus_refspec_excluded(self, guard_module):
        # +feat is force shorthand → skipped (force is handled in a separate arm).
        assert guard_module._push_positionals(["git", "push", "origin", "+feat"]) == ["origin"]

    def test_not_a_push(self, guard_module):
        assert guard_module._push_positionals(["git", "commit", "-m", "x"]) == []


class TestPushConfigIsSimple:
    """_push_config_is_simple: True only when NO config broadens a bare/remote-only
    push — no `remote.<remote>.push` refspec, push.default in {unset, simple,
    current}, and no submodule push-recursion. Fail-CLOSED on any config-read
    error / unresolved remote."""

    def test_all_unset_is_simple(self, guard_module):
        with patch.object(guard_module.subprocess, "run", side_effect=_config_run({})):
            assert guard_module._push_config_is_simple("origin") is True

    def test_custom_push_refspec_not_simple(self, guard_module):
        with patch.object(
            guard_module.subprocess,
            "run",
            side_effect=_config_run({"remote.origin.push": (0, "HEAD:main")}),
        ):
            assert guard_module._push_config_is_simple("origin") is False

    def test_push_default_simple_ok(self, guard_module):
        with patch.object(
            guard_module.subprocess, "run", side_effect=_config_run({"push.default": (0, "simple")})
        ):
            assert guard_module._push_config_is_simple("origin") is True

    def test_push_default_current_ok(self, guard_module):
        with patch.object(
            guard_module.subprocess,
            "run",
            side_effect=_config_run({"push.default": (0, "current")}),
        ):
            assert guard_module._push_config_is_simple("origin") is True

    def test_push_default_matching_not_simple(self, guard_module):
        with patch.object(
            guard_module.subprocess,
            "run",
            side_effect=_config_run({"push.default": (0, "matching")}),
        ):
            assert guard_module._push_config_is_simple("origin") is False

    def test_push_default_upstream_not_simple(self, guard_module):
        # Regression (round-3 BLOCKER): upstream pushes cur to a possibly
        # differently-named upstream branch (e.g. feat → origin/main).
        with patch.object(
            guard_module.subprocess,
            "run",
            side_effect=_config_run({"push.default": (0, "upstream")}),
        ):
            assert guard_module._push_config_is_simple("origin") is False

    def test_push_default_tracking_not_simple(self, guard_module):
        with patch.object(
            guard_module.subprocess,
            "run",
            side_effect=_config_run({"push.default": (0, "tracking")}),
        ):
            assert guard_module._push_config_is_simple("origin") is False

    def test_recurse_submodules_on_demand_not_simple(self, guard_module):
        with patch.object(
            guard_module.subprocess,
            "run",
            side_effect=_config_run({"push.recurseSubmodules": (0, "on-demand")}),
        ):
            assert guard_module._push_config_is_simple("origin") is False

    def test_recurse_submodules_check_ok(self, guard_module):
        with patch.object(
            guard_module.subprocess,
            "run",
            side_effect=_config_run({"push.recurseSubmodules": (0, "check")}),
        ):
            assert guard_module._push_config_is_simple("origin") is True

    def test_recurse_submodules_false_ok(self, guard_module):
        # `false` is git-equivalent to `no` → safe (was wrongly rejected before).
        with patch.object(
            guard_module.subprocess,
            "run",
            side_effect=_config_run({"push.recurseSubmodules": (0, "false")}),
        ):
            assert guard_module._push_config_is_simple("origin") is True

    def test_push_default_case_insensitive(self, guard_module):
        # git enum values are case-insensitive — Matching must still be rejected.
        with patch.object(
            guard_module.subprocess,
            "run",
            side_effect=_config_run({"push.default": (0, "Matching")}),
        ):
            assert guard_module._push_config_is_simple("origin") is False

    def test_mirror_true_not_simple(self, guard_module):
        # P1: remote.<remote>.mirror=true → a bare push mirrors ALL refs → not simple.
        with patch.object(
            guard_module.subprocess,
            "run",
            side_effect=_config_run({"remote.origin.mirror": (0, "true")}),
        ):
            assert guard_module._push_config_is_simple("origin") is False

    def test_submodule_recurse_true_not_simple(self, guard_module):
        with patch.object(
            guard_module.subprocess,
            "run",
            side_effect=_config_run({"submodule.recurse": (0, "true")}),
        ):
            assert guard_module._push_config_is_simple("origin") is False

    def test_config_read_error_fails_closed(self, guard_module):
        # rc 128 (bad config) with empty stdout must NOT be read as "unset".
        with patch.object(
            guard_module.subprocess,
            "run",
            side_effect=_config_run({"remote.origin.push": (128, "")}),
        ):
            assert guard_module._push_config_is_simple("origin") is False

    def test_none_remote_not_simple(self, guard_module):
        with patch.object(guard_module.subprocess, "run", side_effect=AssertionError):
            assert guard_module._push_config_is_simple(None) is False

    def test_exception_fails_closed(self, guard_module):
        with patch.object(guard_module.subprocess, "run", side_effect=OSError("boom")):
            assert guard_module._push_config_is_simple("origin") is False


class TestPushTargetsCurrentBranch:
    """_push_targets_current_branch(seg, cur, remote) (ALLOWLIST posture) is True
    ONLY for a plain current-branch update: bare / `<remote>` (with simple config)
    or explicit `<remote> <cur>`, carrying only ref-neutral flags. `remote` is the
    caller-resolved effective push remote. Every cross-name / delete / broadening /
    unknown-flag / bundled-delete form is False."""

    def _t(self, guard_module, cmd, cur):
        return guard_module._push_targets_current_branch(_push_seg(cmd), cur, "origin")

    # ── explicit `<remote> <cur>` → no config subprocess ──

    def test_explicit_current_branch(self, guard_module):
        assert self._t(guard_module, "git push origin feat", "feat") is True

    def test_set_upstream_current_branch(self, guard_module):
        assert self._t(guard_module, "git push -u origin feat", "feat") is True

    def test_safe_short_bundle_ok(self, guard_module):
        # -uv = --set-upstream --verbose (ref-neutral) with explicit current branch.
        assert self._t(guard_module, "git push -uv origin feat", "feat") is True

    def test_push_option_value_flag_ok(self, guard_module):
        assert self._t(guard_module, "git push -o ci.skip origin feat", "feat") is True

    def test_push_option_glued_ok(self, guard_module):
        assert self._t(guard_module, "git push -oci.skip origin feat", "feat") is True

    def test_different_named_branch_false(self, guard_module):
        # `git push -u origin other` while on feat → NOT a current-branch update.
        assert self._t(guard_module, "git push -u origin other", "feat") is False

    def test_colon_refspec_false(self, guard_module):
        assert self._t(guard_module, "git push origin feat:main", "feat") is False

    def test_delete_refspec_false(self, guard_module):
        assert self._t(guard_module, "git push origin :feat", "feat") is False

    # ── broadening / unknown / bundled-delete flags → False (allowlist rejects) ──

    def test_broadening_all_false(self, guard_module):
        assert self._t(guard_module, "git push --all origin", "feat") is False

    def test_broadening_tags_false(self, guard_module):
        assert self._t(guard_module, "git push --tags origin", "feat") is False

    def test_broadening_delete_flag_false(self, guard_module):
        assert self._t(guard_module, "git push --delete origin feat", "feat") is False

    def test_broadening_repo_flag_false(self, guard_module):
        assert self._t(guard_module, "git push --repo origin feat", "feat") is False

    def test_stdin_flag_false(self, guard_module):
        assert self._t(guard_module, "git push --stdin origin", "feat") is False

    def test_follow_tags_false(self, guard_module):
        assert self._t(guard_module, "git push --follow-tags origin", "feat") is False

    def test_receive_pack_flag_false(self, guard_module):
        # P1: --receive-pack/--exec select an EXECUTED program → not a plain re-push.
        assert self._t(guard_module, "git push --receive-pack=/x origin feat", "feat") is False
        assert self._t(guard_module, "git push --receive-pack /x origin feat", "feat") is False

    def test_exec_flag_false(self, guard_module):
        assert self._t(guard_module, "git push --exec=/x origin feat", "feat") is False

    def test_bundled_delete_false(self, guard_module):
        # -ud / -du / -qd all carry `d` (delete) → rejected by the short-letter scan.
        for cmd in ("git push -ud origin feat", "git push -du origin feat", "git push -qd origin feat"):
            assert self._t(guard_module, cmd, "feat") is False, cmd

    # ── bare / remote-only → gated on _push_config_is_simple (mocked) ──

    def test_bare_push_simple_config_true(self, guard_module):
        with patch.object(guard_module, "_push_config_is_simple", return_value=True):
            assert self._t(guard_module, "git push", "feat") is True

    def test_bare_push_nonsimple_config_false(self, guard_module):
        with patch.object(guard_module, "_push_config_is_simple", return_value=False):
            assert self._t(guard_module, "git push", "feat") is False

    def test_remote_only_uses_effective_remote(self, guard_module):
        # The config check keys on the caller-resolved `remote`, not positionals[0].
        with patch.object(guard_module, "_push_config_is_simple", return_value=True) as cfg:
            assert self._t(guard_module, "git push origin", "feat") is True
        assert cfg.call_args_list[0].args[0] == "origin"

    def test_detached_or_empty_false(self, guard_module):
        assert self._t(guard_module, "git push", "") is False
        assert self._t(guard_module, "git push", None) is False


class TestGetPushRemoteAndBranch:
    """_get_push_remote_and_branch resolves (remote, dst-branch) from the argv —
    fixing the naive-split bug where -u/--set-upstream ate the remote token."""

    def test_set_upstream_does_not_eat_remote(self, guard_module):
        # Regression: `git push -u origin feat` → remote=origin, branch=feat (NOT
        # the current branch via the eaten-remote bug).
        assert guard_module._get_push_remote_and_branch(_push_seg("git push -u origin feat")) == (
            "origin",
            "feat",
        )

    def test_refspec_dst(self, guard_module):
        assert guard_module._get_push_remote_and_branch(
            _push_seg("git push origin feat:main")
        ) == ("origin", "main")

    def test_bare_push_uses_current_branch(self, guard_module):
        with patch.object(guard_module, "_current_branch", return_value="feat"):
            assert guard_module._get_push_remote_and_branch(_push_seg("git push")) == (
                "upstream",
                "feat",
            )

    def test_remote_only_uses_current_branch(self, guard_module):
        with patch.object(guard_module, "_current_branch", return_value="feat"):
            assert guard_module._get_push_remote_and_branch(_push_seg("git push origin")) == (
                "origin",
                "feat",
            )

    def test_value_flag_not_mistaken_for_remote(self, guard_module):
        assert guard_module._get_push_remote_and_branch(
            _push_seg("git push -o ci.skip origin feat")
        ) == ("origin", "feat")

    def test_not_a_push_returns_none(self, guard_module):
        seg = next(iter(analyze("git commit -m x")))
        assert guard_module._get_push_remote_and_branch(seg) == (None, None)


class TestEffectivePushRemote:
    """_effective_push_remote follows git's real bare-push remote precedence:
    explicit --repo/positional > branch.<cur>.pushRemote > remote.pushDefault >
    @{upstream} remote > origin."""

    def test_explicit_positional_remote(self, guard_module):
        with patch.object(guard_module.subprocess, "run", side_effect=AssertionError):
            assert (
                guard_module._effective_push_remote(_push_seg("git push origin"), "feat") == "origin"
            )

    def test_explicit_repo_flag(self, guard_module):
        with patch.object(guard_module.subprocess, "run", side_effect=AssertionError):
            assert (
                guard_module._effective_push_remote(_push_seg("git push --repo fork feat"), "feat")
                == "fork"
            )

    def test_bare_uses_push_remote(self, guard_module):
        # Regression (round-4 BLOCKER): triangular fork workflow — bare push goes to
        # branch.<cur>.pushRemote, NOT the @{upstream} remote.
        with patch.object(
            guard_module.subprocess,
            "run",
            side_effect=_config_run({"branch.feat.pushRemote": (0, "fork")}),
        ):
            assert guard_module._effective_push_remote(_push_seg("git push"), "feat") == "fork"

    def test_bare_uses_push_default(self, guard_module):
        with patch.object(
            guard_module.subprocess,
            "run",
            side_effect=_config_run({"remote.pushDefault": (0, "fork")}),
        ):
            assert guard_module._effective_push_remote(_push_seg("git push"), "feat") == "fork"

    def test_bare_falls_back_to_upstream(self, guard_module):
        with patch.object(
            guard_module.subprocess,
            "run",
            side_effect=_config_run({"@{upstream}": (0, "up/feat")}),
        ):
            assert guard_module._effective_push_remote(_push_seg("git push"), "feat") == "up"

    def test_bare_defaults_to_origin(self, guard_module):
        with patch.object(guard_module.subprocess, "run", side_effect=_config_run({})):
            assert guard_module._effective_push_remote(_push_seg("git push"), "feat") == "origin"


# ── _check_pr_review_findings tests ─────────────────────────────────


class TestCheckPrReviewFindings:
    """Unit tests for _check_pr_review_findings()."""

    def _make_gh_output(self, comments: list[tuple[str, str, str]]) -> str:
        """Build mock ``gh api --paginate --jq '.[] | {…}'`` output: one compact
        JSON object per line (JSONL), matching the per-element jq the code now
        uses. An empty list yields "" (gh emits nothing for zero comments)."""
        return "\n".join(
            json.dumps({"login": login, "type": utype, "body": body})
            for login, utype, body in comments
        )

    def test_no_comments_allows_merge(self, guard_module):
        """No review comments at all → fail-open (quota exhausted case)."""
        with patch.object(guard_module.subprocess, "run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="",
                stderr="",
            )
            should_block, msg = guard_module._check_pr_review_findings("100")
        assert not should_block
        assert msg == ""

    def test_paginated_multiline_findings_parsed_and_blocked(self, guard_module):
        """Regression: issues/N/comments must be fetched with --paginate and
        parsed as per-line JSONL.

        Without --paginate a [P1]/ERROR beyond the first REST page (30 comments)
        was silently dropped and the merge — this scan runs on the live merge
        path with strict=False — sailed through; the old whole-body json.loads
        also choked on the JSONL that --paginate emits (→ _scan_unreadable →
        fail-open). Feed a multi-line JSONL response whose most-recent comment
        carries an ERROR and assert the gate blocks AND that --paginate is
        actually requested.
        """
        output = self._make_gh_output(
            [
                ("chatgpt-codex-connector[bot]", "Bot", "## Structural Review\n\nEarlier note."),
                ("chatgpt-codex-connector[bot]", "Bot", "### ERROR — raw SQL in production code"),
            ]
        )
        assert "\n" in output  # guard: genuinely multi-line JSONL, not a single array blob
        with patch.object(guard_module.subprocess, "run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=output, stderr="",
            )
            should_block, _ = guard_module._check_pr_review_findings("100")
            gh_argv = mock_run.call_args.args[0]
        assert should_block
        # Paged fetch requests explicit query params (per_page/page), not --paginate.
        assert "per_page=100" in gh_argv
        assert "page=1" in gh_argv

    def test_clean_review_allows_merge(self, guard_module):
        """Review comment with CLEAN verdict → allow."""
        output = self._make_gh_output(
            [
                (
                    "chatgpt-codex-connector[bot]",
                    "Bot",
                    "## Structural Review\n\nNo issues.\n\n## PII / Secrets / Wording scan: **CLEAN**",
                ),
            ]
        )
        with patch.object(guard_module.subprocess, "run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=output,
                stderr="",
            )
            should_block, msg = guard_module._check_pr_review_findings("100")
        assert not should_block

    def test_error_finding_blocks_merge(self, guard_module):
        """Review with ### ERROR blocks merge."""
        output = self._make_gh_output(
            [
                (
                    "chatgpt-codex-connector[bot]",
                    "Bot",
                    "## Structural Review\n\n### ERROR — Raw SQL in production code\n\n"
                    "`src/genesis/foo.py` uses raw SQL.\n\n"
                    "## PII scan: not performed",
                ),
            ]
        )
        with patch.object(guard_module.subprocess, "run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=output,
                stderr="",
            )
            should_block, msg = guard_module._check_pr_review_findings("100")
        assert should_block
        assert "review-override" in msg

    def test_p1_finding_blocks_merge(self, guard_module):
        """Review with [P1] marker blocks merge."""
        output = self._make_gh_output(
            [
                (
                    "chatgpt-codex-connector[bot]",
                    "Bot",
                    "[P1] Logic bug: session_id always None\n[P2] Missing docstring on helper",
                ),
            ]
        )
        with patch.object(guard_module.subprocess, "run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=output,
                stderr="",
            )
            should_block, msg = guard_module._check_pr_review_findings("100")
        assert should_block

    def test_hard_block_blocks_merge(self, guard_module):
        """Review with HARD BLOCK blocks merge."""
        output = self._make_gh_output(
            [
                (
                    "chatgpt-codex-connector[bot]",
                    "Bot",
                    "### 🔴 HARD BLOCK\n\nPrivate IP found in config file.",
                ),
            ]
        )
        with patch.object(guard_module.subprocess, "run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=output,
                stderr="",
            )
            should_block, msg = guard_module._check_pr_review_findings("100")
        assert should_block

    def test_warning_only_allows_merge(self, guard_module):
        """Review with only WARNINGs (no ERRORs) → allow."""
        output = self._make_gh_output(
            [
                (
                    "chatgpt-codex-connector[bot]",
                    "Bot",
                    "## Structural Review\n\n### WARNING — Missing test coverage\n\n"
                    "No test for new function.\n\n"
                    "## PII / Secrets / Wording scan: **CLEAN**",
                ),
            ]
        )
        with patch.object(guard_module.subprocess, "run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=output,
                stderr="",
            )
            should_block, msg = guard_module._check_pr_review_findings("100")
        assert not should_block

    def test_force_override_allows_merge(self, guard_module):
        """Force override skips the check entirely."""
        should_block, msg = guard_module._check_pr_review_findings("100", force=True)
        assert not should_block

    def test_api_error_fails_closed(self, guard_module):
        """gh api returning error → fail-CLOSED (block; unreadable scan, PR #1434)."""
        with patch.object(guard_module.subprocess, "run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=1,
                stdout="",
                stderr="API error",
            )
            should_block, msg = guard_module._check_pr_review_findings("100")
        assert should_block
        assert "UNREADABLE" in msg

    def test_api_timeout_fails_closed(self, guard_module):
        """gh api timeout → fail-CLOSED (block; unreadable scan, PR #1434)."""
        with patch.object(guard_module.subprocess, "run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd="gh", timeout=15)
            should_block, msg = guard_module._check_pr_review_findings("100")
        assert should_block
        assert "UNREADABLE" in msg

    def test_newer_clean_review_overrides_old_error(self, guard_module):
        """When latest bot comment is clean, old ERROR is considered resolved."""
        output = self._make_gh_output(
            [
                (
                    "chatgpt-codex-connector[bot]",
                    "Bot",
                    "## Structural Review\n\n### ERROR — Raw SQL\n\nFix needed.",
                ),
                (
                    "chatgpt-codex-connector[bot]",
                    "Bot",
                    "## Structural Review\n\nPASS — no issues.\n\n"
                    "## PII / Secrets / Wording scan: **CLEAN**\n\nVERDICT: PASS",
                ),
            ]
        )
        with patch.object(guard_module.subprocess, "run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=output,
                stderr="",
            )
            should_block, msg = guard_module._check_pr_review_findings("100")
        assert not should_block

    def test_human_comments_ignored(self, guard_module):
        """Human comments with 'ERROR' in text are not checked."""
        output = self._make_gh_output(
            [
                ("octocat", "User", "### ERROR — I think this is wrong\n\nJust my opinion."),
            ]
        )
        with patch.object(guard_module.subprocess, "run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=output,
                stderr="",
            )
            should_block, msg = guard_module._check_pr_review_findings("100")
        assert not should_block

    def test_codex_quota_message_not_treated_as_review(self, guard_module):
        """Codex quota-exhausted message without findings → not a review."""
        output = self._make_gh_output(
            [
                (
                    "chatgpt-codex-connector[bot]",
                    "Bot",
                    "You have reached your Codex usage limits for code reviews. "
                    "You can see your limits in the Codex usage dashboard.",
                ),
            ]
        )
        with patch.object(guard_module.subprocess, "run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=output,
                stderr="",
            )
            should_block, msg = guard_module._check_pr_review_findings("100")
        assert not should_block

    def test_error_in_clean_comment_does_not_block(self, guard_module):
        """Comment mentions ERROR category but scan is CLEAN → no block."""
        output = self._make_gh_output(
            [
                (
                    "chatgpt-codex-connector[bot]",
                    "Bot",
                    "**Structural review:** No ERRORs found.\n\n"
                    "## PII / Secrets / Wording scan: **CLEAN**",
                ),
            ]
        )
        with patch.object(guard_module.subprocess, "run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=output,
                stderr="",
            )
            should_block, msg = guard_module._check_pr_review_findings("100")
        assert not should_block

    def test_error_plus_incidental_clean_phrase_blocks(self, guard_module):
        """A real ERROR heading with incidental prose should still block."""
        output = self._make_gh_output(
            [
                (
                    "chatgpt-codex-connector[bot]",
                    "Bot",
                    "### ERROR — hardcoded credential\n\nUse env vars.\n\n"
                    "No issues found in the formatting section.",
                ),
            ]
        )
        with patch.object(guard_module.subprocess, "run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=output,
                stderr="",
            )
            should_block, msg = guard_module._check_pr_review_findings("100")
        assert should_block

    def test_null_body_fails_open(self, guard_module):
        """GitHub API returning body: null should not crash."""
        # JSONL (one object per line) to match the per-element --jq the code uses;
        # body: null must coerce to "" rather than crash.
        output = json.dumps(
            {"login": "chatgpt-codex-connector[bot]", "type": "Bot", "body": None}
        )
        with patch.object(guard_module.subprocess, "run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=output,
                stderr="",
            )
            should_block, msg = guard_module._check_pr_review_findings("100")
        assert not should_block

    def test_p2_only_allows_merge(self, guard_module):
        """Review with only [P2] markers → allow."""
        output = self._make_gh_output(
            [
                (
                    "chatgpt-codex-connector[bot]",
                    "Bot",
                    "[P2] Missing docstring\n[P2] Inline import",
                ),
            ]
        )
        with patch.object(guard_module.subprocess, "run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=output,
                stderr="",
            )
            should_block, msg = guard_module._check_pr_review_findings("100")
        assert not should_block


# ── Integration: full hook invocation ────────────────────────────────


class TestMergeGateIntegration:
    """Test the full hook via subprocess invocation."""

    def test_non_merge_command_passes_through(self):
        """Regular bash commands are not affected by merge gate."""
        result = _run_guard("ls -la")
        assert result.returncode == 0

    def test_merge_without_admin_blocked(self):
        """gh pr merge without --admin is always blocked."""
        result = _run_guard("gh pr merge 100 --squash")
        assert result.returncode == 2
        assert "--admin" in result.stderr

    def test_sqlite3_write_blocked(self):
        """sqlite3 with write operations is hard blocked."""
        result = _run_guard('sqlite3 genesis.db "DELETE FROM knowledge_units"')
        assert result.returncode == 2
        assert "sqlite3" in result.stderr.lower() or "database" in result.stderr.lower()

    def test_sqlite3_read_allowed(self):
        """sqlite3 with SELECT is allowed."""
        result = _run_guard('sqlite3 genesis.db "SELECT COUNT(*) FROM observations"')
        assert result.returncode == 0

    def test_git_commit_no_verify_blocked(self):
        """git commit --no-verify is hard blocked."""
        result = _run_guard('git commit --no-verify -m "bypass"')
        assert result.returncode == 2
        assert "no-verify" in result.stderr.lower()

    def test_kill_command_warns(self):
        """kill command produces a soft warning (exit 0)."""
        result = _run_guard("kill -9 12345")
        assert result.returncode == 0
        assert "kill" in result.stderr.lower() or "process" in result.stderr.lower()

    def test_git_config_write_warns(self):
        """git config set produces a soft warning."""
        result = _run_guard("git config core.hooksPath /tmp/evil")
        assert result.returncode == 0
        assert "config" in result.stderr.lower()

    def test_git_config_read_silent(self):
        """git config --get does not warn."""
        result = _run_guard("git config --get user.name")
        assert result.returncode == 0
        assert result.stderr.strip() == ""


# ── _check_inline_review_findings tests ──────────────────────────────

# CodeRabbit severity headers, copied VERBATIM from live PRs rather than written
# from memory — the format is what the matcher keys on, so a paraphrase would
# test the paraphrase. Sources: PR #1615 and #1621 (Major), PR #1603 (Minor).
_CR_MAJOR_BODY = (
    "_🔒 Security & Privacy_ | _🟠 Major_ | _🏗️ Heavy lift_\n\n"
    "**Track unresolved reparsing prefixes through wrapper chains.**\n\n"
    "Line 339 only checks the first raw word."
)
_CR_MINOR_BODY = (
    "_🎯 Functional Correctness_ | _🟡 Minor_ | _⚡ Quick win_\n\n"
    "**Prefer an explicit tie-break.**\n\nDetails here."
)
_CR_CRITICAL_BODY = (
    "_🗄️ Data Integrity_ | _🔴 Critical_ | _🏗️ Heavy lift_\n\n"
    "**Data loss on concurrent write.**\n\nDetails here."
)
# The parse trap, observed live: the header is NOT always three fields. This one
# omits the severity entirely, so a POSITIONAL regex reads the effort field as
# the severity and reports "Heavy lift" as a blocking level.
_CR_NO_SEVERITY_BODY = (
    "_📐 Maintainability_ | _🏗️ Heavy lift_\n\n**Consider extracting a helper.**"
)

_P1_BODY = (
    "**<sub><sub>![P1 Badge](https://img.shields.io/badge/P1-red)"
    "</sub></sub>  Make queue claim atomic across concurrent pollers**"
    "\n\nDetails here."
)
_P2_BODY = (
    "**<sub><sub>![P2 Badge](https://img.shields.io/badge/P2-yellow)"
    "</sub></sub>  Preserve multi-word ledger keys for entity lookup**"
    "\n\nDetails here."
)


class TestCheckInlineReviewFindings:
    """Inline (pulls/N/comments) findings: P1 blocks, P2 warns.

    Codex posts findings ONLY on this endpoint; the review body is
    boilerplate — 173 findings passed the gate unseen before this
    (2026-07-10 audit)."""

    def _mock(self, guard_module, comments, rc=0):
        # gh api --paginate with a per-element jq filter emits one
        # compact JSON object per line across ALL result pages.
        return patch.object(
            guard_module.subprocess,
            "run",
            return_value=subprocess.CompletedProcess(
                args=[],
                returncode=rc,
                stdout="\n".join(json.dumps(c) for c in comments),
                stderr="",
            ),
        )

    def _codex(self, cid, body, reply_to=None, path=None):
        d = {
            "id": cid,
            "reply_to": reply_to,
            "login": "chatgpt-codex-connector[bot]",
            "type": "Bot",
            "body": body,
        }
        if path is not None:
            d["path"] = path
        return d

    def _coderabbit(self, cid, body, reply_to=None, path=None):
        d = {
            "id": cid,
            "reply_to": reply_to,
            "login": "coderabbitai[bot]",
            "type": "Bot",
            "body": body,
        }
        if path is not None:
            d["path"] = path
        return d

    # ── CodeRabbit findings, which the gate read and then silently dropped ──
    #
    # The failure was never at the fetch or author layer, and locating it wrongly
    # sends the fix to the wrong place. MEASURED on live PR #1615: the comment has
    # `user.type == "Bot"`, so it PASSES the permissive author filter, is fetched,
    # and is iterated in the same loop as the Codex findings. It then reaches
    # `if P1 / elif P2`, matches neither, falls out with no `else`, and scores 0.
    # Read, then dropped — and `inline-findings: ok` on a PR carrying a Major
    # reads exactly like `inline-findings: ok` on a clean one.

    def test_coderabbit_major_blocks(self, guard_module):
        """The KNOWN POSITIVE, verbatim from live PR #1615.

        This case is the acceptance bar for the whole matcher: a matcher that
        finds nothing is indistinguishable from one that looks at nothing, so a
        real finding must be shown to be caught before any negative result here
        is trusted.
        """
        with self._mock(guard_module, [self._coderabbit(1, _CR_MAJOR_BODY)]):
            block, msg = guard_module._check_inline_review_findings("100")
        assert block, "a Security & Privacy Major was read and then dropped"
        assert "Track unresolved reparsing prefixes" in msg

    def test_coderabbit_critical_blocks(self, guard_module):
        with self._mock(guard_module, [self._coderabbit(1, _CR_CRITICAL_BODY)]):
            block, _ = guard_module._check_inline_review_findings("100")
        assert block

    def test_coderabbit_minor_does_not_block(self, guard_module):
        """Scoring policy: Critical and Major score 1.0, EVERY other level 0.

        Minor is by far the most common level observed (73 of 104 findings), so
        blocking on it would train reflexive overrides — which is worse than not
        gating at all.
        """
        with self._mock(guard_module, [self._coderabbit(1, _CR_MINOR_BODY)]):
            block, _ = guard_module._check_inline_review_findings("100")
        assert not block

    def test_coderabbit_absent_severity_field_does_not_block(self, guard_module):
        """The parse trap, observed live: the header is not always three fields.

        A POSITIONAL regex reads the effort field as the severity here and would
        report "Heavy lift" as a blocking level. Matching the severity WORD inside
        its own delimited span is what makes this case fall through correctly.
        """
        with self._mock(guard_module, [self._coderabbit(1, _CR_NO_SEVERITY_BODY)]):
            block, _ = guard_module._check_inline_review_findings("100")
        assert not block

    def test_coderabbit_major_on_doc_path_does_not_block(self, guard_module):
        """Same doc-path treatment the Codex path already gets.

        Without this the two reviewers are inconsistent: a finding on CHANGELOG.md
        would block from one and not the other, for no stated reason.
        """
        with self._mock(
            guard_module, [self._coderabbit(1, _CR_MAJOR_BODY, path="CHANGELOG.md")]
        ):
            block, _ = guard_module._check_inline_review_findings("100")
        assert not block

    # ── the body is a CODE-BEARING document, so the severity read is anchored ──
    #
    # A review comment quotes the diff and embeds ```suggestion``` blocks, and `_`
    # is at once CodeRabbit's severity delimiter, markdown emphasis, AND the
    # snake_case separator. A body-wide search for a `_`-delimited severity word
    # therefore matches ordinary source: MEASURED at 152 such tokens in this repo,
    # including this feature's own `_CR_MAJOR_BODY` fixture. Each case below is a
    # real shape from that measurement, and each one BLOCKED before the read was
    # anchored to the header line — a *Minor* finding reported as "Critical/Major".

    @pytest.mark.parametrize(
        "quoted,label",
        [
            ("```suggestion\nMAX_CRITICAL_ERRORS = 5\n```", "a constant in a suggestion"),
            ("```python\ndef is_major_bump(x):\n    ...\n```", "a snake_case function"),
            ("This is a _major_ readability win.", "italic prose"),
            ("This is _not critical_ but worth noting.", "a NEGATION"),
            ("See `runtime_critical_path.py` for context.", "a file path"),
            ("The constant `_CR_MAJOR_BODY` is the fixture.", "this feature's own fixture"),
        ],
    )
    def test_coderabbit_minor_quoting_severity_word_does_not_block(
        self, guard_module, quoted, label
    ):
        """A Minor finding whose BODY quotes a severity word must not block."""
        body = f"_🟡 Minor_ | _⚡ Quick win_\n\n**A trivial nit.**\n\n{quoted}"
        assert guard_module._cr_severity(body)[0] == "minor", label
        with self._mock(guard_module, [self._coderabbit(1, body)]):
            block, _ = guard_module._check_inline_review_findings("100")
        assert not block, f"{label} in the body was read as the severity"

    # ── one COMMENT is not one FINDING ────────────────────────────────────
    #
    # CodeRabbit bundles several findings into a single inline comment,
    # separated by a markdown rule, when they land near each other in the diff.
    # Reading only the first is this feature's ORIGINAL BUG one level down:
    # read, then silently dropped. MEASURED on live PR #1647 comment 3925021846
    # — one comment, two distinct Major findings, of which the gate saw one.

    def test_bundled_findings_are_each_scored(self, guard_module):
        """A Major bundled AFTER a Minor must not disappear.

        This ordering is the dangerous one: the comment scores as `minor`, lands
        in the advisory list, and prints "below Major, NOT counted" — a false
        claim about a Major that was never read at all.
        """
        body = (
            "_📐 Maintainability_ | _🟡 Minor_ | _⚡ Quick win_\n\n"
            "**Prefer an explicit tie-break.**\n\n"
            "---\n\n"
            "_🗄️ Data Integrity_ | _🟠 Major_ | _🏗️ Heavy lift_\n\n"
            "**Data loss on concurrent write.**\n"
        )
        assert [guard_module._cr_severity(s)[0] for s in guard_module._cr_findings(body)] == [
            "minor",
            "major",
        ]
        with self._mock(guard_module, [self._coderabbit(1, body)]):
            block, msg = guard_module._check_inline_review_findings("100")
        assert block, "a Major bundled after a Minor was dropped"
        assert "Data loss on concurrent write" in msg

    def test_two_bundled_majors_score_twice(self, guard_module):
        """Scoring the COMMENT instead of each finding undercounts."""
        body = (
            "_🗄️ Data Integrity_ | _🟠 Major_ | _🏗️ Heavy lift_\n\n**First problem.**\n\n"
            "---\n\n"
            "_🎯 Functional Correctness_ | _🟠 Major_ | _⚡ Quick win_\n\n**Second problem.**\n"
        )
        with self._mock(guard_module, [self._coderabbit(1, body)]):
            block, msg = guard_module._check_inline_review_findings("100")
        assert block
        assert "First problem" in msg and "Second problem" in msg
        assert "2 CodeRabbit" in msg

    def test_rule_in_prose_does_not_invent_a_finding(self, guard_module):
        """A markdown rule is only a boundary when a header follows it.

        Splitting on every rule would fabricate findings out of ordinary prose,
        which is the opposite failure and just as wrong.
        """
        body = (
            "_🎯 Correctness_ | _🟡 Minor_ | _⚡ Quick win_\n\n**A nit.**\n\n"
            "---\n\nJust some trailing prose, not a finding.\n"
        )
        assert len(guard_module._cr_findings(body)) == 1

    def test_oversized_category_field_keeps_its_severity(self, guard_module):
        """The field bound must not sit exactly on live data.

        MEASURED across 124 real findings, the longest category is
        `📐 Maintainability & Code Quality` at EXACTLY 32 characters. A 32-char
        bound therefore has zero margin: one vendor rename and a genuine Major
        stops parsing and is reported as "below Major".
        """
        for cat in ("📐 Maintainability & Code Quality", "📐 Maintainability & Code Quality Now"):
            body = f"_{cat}_ | _🟠 Major_ | _🏗️ Heavy lift_\n\n**Something.**"
            assert guard_module._cr_severity(body)[0] == "major", f"len {len(cat)} lost its Major"

    def test_header_shaped_but_unparseable_is_a_canary(self, guard_module, capsys):
        """"Not a header" and "a header I could not parse" are different.

        Conflating them prints an unparsed finding as "below Major" — a
        confident false statement about a level that was never read.
        """
        body = "_🔒 Security | Privacy_ | _🟠 Major_ | _🏗️ x_\n\n**Something.**"
        severity, header_seen = guard_module._cr_severity(body)
        assert (severity, header_seen) == (None, True)
        with self._mock(guard_module, [self._coderabbit(1, body)]):
            guard_module._check_inline_review_findings("100")
        assert "unknown-severity" in capsys.readouterr().err

    def test_coderabbit_unknown_severity_fails_open_with_canary(
        self, guard_module, capsys
    ):
        """A level this gate does not know must NOT silently start blocking.

        Failing closed here would wedge every merge the moment the vendor adds a
        rung to the ladder. It is surfaced instead, so an unrecognised level is
        never invisible — which is the whole point of this PR.
        """
        body = "_🔒 Security_ | _🟣 Catastrophic_ | _🏗️ Heavy lift_\n\n**Something.**"
        severity, header_seen = guard_module._cr_severity(body)
        assert (severity, header_seen) == (None, True)
        with self._mock(guard_module, [self._coderabbit(1, body)]):
            block, _ = guard_module._check_inline_review_findings("100")
        assert not block
        assert "unknown-severity" in capsys.readouterr().err

    def test_coderabbit_major_replied_by_maintainer_does_not_block(self, guard_module):
        """The line that turns a BLOCK into a PASS, which had no test.

        A maintainer reply is a conscious acceptance and clears the finding — so
        this branch can silence a real Major, and an untested branch that can
        silence a Major is the one most worth locking.
        """
        with self._mock(
            guard_module,
            [
                self._coderabbit(1, _CR_MAJOR_BODY),
                {
                    "id": 2,
                    "reply_to": 1,
                    "login": "a-maintainer",
                    "type": "User",
                    "assoc": "OWNER",
                    "body": "Accepted — tracked separately.",
                },
            ],
        ):
            block, _ = guard_module._check_inline_review_findings("100")
        assert not block

    def test_coderabbit_major_reply_from_non_maintainer_still_blocks(self, guard_module):
        """A drive-by reply must NOT be able to silence a Major."""
        with self._mock(
            guard_module,
            [
                self._coderabbit(1, _CR_MAJOR_BODY),
                {
                    "id": 2,
                    "reply_to": 1,
                    "login": "a-passer-by",
                    "type": "User",
                    "assoc": "NONE",
                    "body": "lgtm",
                },
            ],
        ):
            block, _ = guard_module._check_inline_review_findings("100")
        assert block

    def test_unrecognised_review_bot_is_surfaced_not_dropped(self, guard_module, capsys):
        """The silent-drop CLASS, which is not CodeRabbit-specific.

        MEASURED on live PR #1621 while building this: Codex **P3** findings hit
        the same hole — `_INLINE_P1_RE`/`_INLINE_P2_RE` match only P1 and P2, so
        every P3 ever posted fell out of the if/elif with no `else` and scored 0,
        exactly as CodeRabbit's did. `github-advanced-security[bot]` is allowlisted
        today with no matcher at all and would have been next.

        Surfaced, never scored: recognising a format is what earns a weight, and
        guessing a severity from an unknown one would be worse than the blindness.
        """
        p3 = (
            "**<sub><sub>![P3 Badge](https://img.shields.io/badge/P3-lightgrey)"
            "</sub></sub>  Keep extglob parentheses inside the pattern word**"
            "\n\nDetails."
        )
        with self._mock(guard_module, [self._codex(1, p3)]):
            block, _ = guard_module._check_inline_review_findings("100")
        assert not block, "an unrecognised format must not be guessed at"
        err = capsys.readouterr().err
        assert "unrecognised" in err
        assert "Keep extglob parentheses" in err

    def test_coderabbit_title_ignores_bold_inside_quoted_code(self, guard_module):
        """The title must name the FINDING, not something out of a fence.

        CodeRabbit embeds fenced blocks and collapsed `<details>` static-analysis
        output, both of which routinely carry bold text belonging to quoted code.
        Reporting that as the title misnames the finding in the very report a
        human reads to decide a merge.
        """
        body = (
            "_🔒 Security_ | _🟠 Major_ | _🏗️ Heavy lift_\n\n"
            "<details>\n<summary>x</summary>\n\n**Script executed:**\n\n</details>\n\n"
            "```shell\n**not the title**\n```\n\n"
            "**The actual finding title.**\n\nBody."
        )
        assert guard_module._coderabbit_title(body) == "The actual finding title."

    def test_coderabbit_major_combines_with_codex_score(self, guard_module):
        """One CodeRabbit Major (1.0) alone reaches the existing threshold.

        Deliberately fed through #1589's weighted machinery rather than a second
        blocking path, so there is one score and one threshold to reason about.
        """
        with self._mock(
            guard_module,
            [self._coderabbit(1, _CR_MAJOR_BODY), self._codex(2, _P2_BODY)],
        ):
            block, msg = guard_module._check_inline_review_findings("100")
        assert block
        assert "score" in msg.lower()

    def test_inline_p1_blocks_with_title(self, guard_module):
        with self._mock(guard_module, [self._codex(1, _P1_BODY)]):
            block, msg = guard_module._check_inline_review_findings("100")
        assert block
        assert "Make queue claim atomic" in msg

    def test_inline_p2_warns_but_allows(self, guard_module, capsys):
        with self._mock(guard_module, [self._codex(1, _P2_BODY)]):
            block, msg = guard_module._check_inline_review_findings("100")
        assert not block
        err = capsys.readouterr().err
        assert "[P2] Preserve multi-word ledger keys" in err

    # ── weighted review score: P1 = 1.0, P2 = 0.5, block at >= 1.0 ──
    def test_inline_two_p2_block(self, guard_module):
        """Two unresolved P2s score 1.0 >= threshold → BLOCK (a single P2 stays
        advisory; see test_inline_p2_warns_but_allows)."""
        with self._mock(
            guard_module, [self._codex(1, _P2_BODY), self._codex(2, _P2_BODY)]
        ):
            block, msg = guard_module._check_inline_review_findings("100")
        assert block
        assert "score" in msg.lower()

    def test_inline_p1_plus_p2_block_lists_both(self, guard_module):
        """1 P1 (1.0) + 1 P2 (0.5) = 1.5 → BLOCK; the P1 title is listed."""
        with self._mock(
            guard_module, [self._codex(1, _P1_BODY), self._codex(2, _P2_BODY)]
        ):
            block, msg = guard_module._check_inline_review_findings("100")
        assert block
        assert "Make queue claim atomic" in msg

    def test_inline_replied_p2_excluded_from_score(self, guard_module):
        """A MAINTAINER reply acks a P2 (consciously accepted) — it drops from the
        score, so two P2s where one is replied = 0.5 < 1.0 → no block."""
        comments = [
            self._codex(1, _P2_BODY),
            self._codex(2, _P2_BODY),
            {
                "id": 3,
                "reply_to": 2,
                "login": "WingedGuardian",
                "type": "User",
                "assoc": "OWNER",
                "body": "Accepted; tracked in a follow-up.",
            },
        ]
        with self._mock(guard_module, comments):
            block, _ = guard_module._check_inline_review_findings("100")
        assert not block

    def test_inline_doc_path_p2_excluded_from_score(self, guard_module, capsys):
        """A P2 on a documentation path is not a code defect — excluded from the
        score, so two P2s where one is on CHANGELOG.md = 0.5 < 1.0 → no block. But it is
        still SURFACED as a NOTE (never silently dropped — Codex #1589 P2)."""
        comments = [
            self._codex(1, _P2_BODY, path="src/genesis/foo.py"),
            self._codex(2, _P2_BODY, path="CHANGELOG.md"),
        ]
        with self._mock(guard_module, comments):
            block, _ = guard_module._check_inline_review_findings("100")
        assert not block
        # The doc-path P2 must remain VISIBLE in the pre-merge report, not dropped.
        assert "[doc P2]" in capsys.readouterr().err

    def test_replied_p1_is_acknowledged_by_maintainer(self, guard_module):
        """A MAINTAINER reply (author_association OWNER/MEMBER/COLLABORATOR) acks a P1."""
        comments = [
            self._codex(1, _P1_BODY),
            {
                "id": 2,
                "reply_to": 1,
                "login": "WingedGuardian",
                "type": "User",
                "assoc": "OWNER",
                "body": "Fixed in abc123.",
            },
        ]
        with self._mock(guard_module, comments):
            block, _ = guard_module._check_inline_review_findings("100")
        assert not block

    def test_non_maintainer_reply_does_not_acknowledge_p1(self, guard_module):
        """LOW-a: a reply from a non-authority account (NONE / throwaway / PR author
        with no push rights) must NOT silence a real P1 — it still blocks."""
        for assoc in ("NONE", "FIRST_TIME_CONTRIBUTOR", "CONTRIBUTOR", None):
            comments = [
                self._codex(1, _P1_BODY),
                {
                    "id": 2,
                    "reply_to": 1,
                    "login": "drive-by-account",
                    "type": "User",
                    "assoc": assoc,
                    "body": "lgtm",
                },
            ]
            with self._mock(guard_module, comments):
                block, msg = guard_module._check_inline_review_findings("100")
            assert block, f"assoc={assoc!r} should NOT acknowledge the P1"
            assert "Make queue claim atomic" in msg

    def test_force_override_allows(self, guard_module):
        block, _ = guard_module._check_inline_review_findings(
            "100",
            force=True,
        )
        assert not block

    def test_api_error_fails_closed(self, guard_module):
        """Unreadable inline scan → fail-CLOSED (block; PR #1434)."""
        with self._mock(guard_module, [], rc=1):
            block, msg = guard_module._check_inline_review_findings("100")
        assert block
        assert "UNREADABLE" in msg

    def test_gh_call_paginates(self, guard_module):
        # Findings beyond REST page 1 (30 comments) must still gate —
        # the very first PR through this gate (#996) drew a P1 for it.
        # The paged fetch requests explicit per_page/page query params.
        with self._mock(guard_module, []) as run_mock:
            guard_module._check_inline_review_findings("100")
        argv = run_mock.call_args[0][0]
        assert "per_page=100" in argv
        assert "page=1" in argv

    def test_inline_fetch_uses_default_ascending_order(self, guard_module):
        # The inline scan fetches in the endpoint's DEFAULT ascending (oldest-first)
        # order — NO sort/direction params. Descending (newest-first) page-number paging
        # would never revisit page 1, so a P1 appended mid-scan on a >100-comment PR
        # could go unseen (Codex P2). Ascending puts an appended comment on the last
        # page, which sequential pagination reaches.
        with self._mock(guard_module, []) as run_mock:
            guard_module._check_inline_review_findings("100")
        argv = run_mock.call_args[0][0]
        assert "sort=created" not in argv
        assert "direction=desc" not in argv
        assert any("pulls/100/comments" in tok for tok in argv)

    def test_body_fetch_uses_default_ascending_order(self, guard_module):
        with patch.object(
            guard_module.subprocess, "run",
            return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
        ) as run_mock:
            guard_module._check_pr_review_findings("100")
        argv = run_mock.call_args[0][0]
        assert "sort=created" not in argv
        assert "direction=desc" not in argv
        assert any("issues/100/comments" in tok for tok in argv)

    def test_p1_beyond_first_page_blocks(self, guard_module):
        filler = [self._codex(i, "note") for i in range(1, 32)]
        with self._mock(guard_module, [*filler, self._codex(99, _P1_BODY)]):
            block, _ = guard_module._check_inline_review_findings("100")
        assert block

    def test_human_inline_comments_ignored(self, guard_module):
        comments = [
            {
                "id": 1,
                "reply_to": None,
                "login": "WingedGuardian",
                "type": "User",
                "body": _P1_BODY,
            }
        ]
        with self._mock(guard_module, comments):
            block, _ = guard_module._check_inline_review_findings("100")
        # type=User AND not in the inline bot set → not an automated finding
        assert block is False

    # ── doc-path allowlist (ledger 54eb3752) ─────────────────────────────
    # A P1 whose inline finding sits on a DOCUMENTATION file does not block the
    # merge; a code file — including a code file UNDER docs/ — still blocks. Safe
    # default: any path not on the explicit allowlist blocks.
    def test_inline_p1_on_changelog_does_not_block(self, guard_module, capsys):
        with self._mock(guard_module, [self._codex(1, _P1_BODY, path="CHANGELOG.md")]):
            block, _ = guard_module._check_inline_review_findings("100")
        assert not block
        assert "documentation" in capsys.readouterr().err.lower()

    def test_inline_p1_on_readme_does_not_block(self, guard_module):
        with self._mock(guard_module, [self._codex(1, _P1_BODY, path="README.md")]):
            block, _ = guard_module._check_inline_review_findings("100")
        assert not block

    def test_inline_p1_under_docs_dir_does_not_block(self, guard_module):
        with self._mock(
            guard_module, [self._codex(1, _P1_BODY, path="docs/architecture/x.md")]
        ):
            block, _ = guard_module._check_inline_review_findings("100")
        assert not block

    def test_inline_p1_on_rst_does_not_block(self, guard_module):
        with self._mock(guard_module, [self._codex(1, _P1_BODY, path="guide.rst")]):
            block, _ = guard_module._check_inline_review_findings("100")
        assert not block

    def test_inline_p1_on_code_path_still_blocks(self, guard_module):
        with self._mock(
            guard_module, [self._codex(1, _P1_BODY, path="src/genesis/foo.py")]
        ):
            block, msg = guard_module._check_inline_review_findings("100")
        assert block
        assert "Make queue claim atomic" in msg

    def test_inline_p1_on_random_md_still_blocks(self, guard_module):
        # Safe default: a non-allowlisted *.md (not CHANGELOG/README, not under
        # docs/) is NOT a doc → still blocks.
        with self._mock(guard_module, [self._codex(1, _P1_BODY, path="NOTES.md")]):
            block, _ = guard_module._check_inline_review_findings("100")
        assert block

    def test_inline_p1_on_code_under_docs_still_blocks(self, guard_module):
        # docs/conf.py is executable Python — a finding there must still block.
        with self._mock(guard_module, [self._codex(1, _P1_BODY, path="docs/conf.py")]):
            block, _ = guard_module._check_inline_review_findings("100")
        assert block

    def test_inline_p1_missing_path_still_blocks(self, guard_module):
        # A finding with no path (malformed/absent) fails SAFE → blocks.
        with self._mock(guard_module, [self._codex(1, _P1_BODY)]):
            block, _ = guard_module._check_inline_review_findings("100")
        assert block

    def test_mixed_doc_and_code_p1_blocks_naming_code(self, guard_module):
        comments = [
            self._codex(1, _P1_BODY, path="CHANGELOG.md"),
            self._codex(
                2,
                "**<sub><sub>![P1 Badge](https://img.shields.io/badge/P1-red)"
                "</sub></sub>  Real code defect in router**\n\nDetails.",
                path="src/genesis/router.py",
            ),
        ]
        with self._mock(guard_module, comments):
            block, msg = guard_module._check_inline_review_findings("100")
        assert block
        assert "Real code defect in router" in msg
        assert "Make queue claim atomic" not in msg  # the doc P1 is not listed

    def test_jq_projects_path(self, guard_module):
        # The test seam bypasses jq, so lock the projection by asserting the argv
        # the production fetch sends includes `path: .path`.
        with self._mock(guard_module, []) as run_mock:
            guard_module._check_inline_review_findings("100")
        argv = run_mock.call_args[0][0]
        assert any("path: .path" in tok for tok in argv)

    # Fail-closed allowlist (security review HIGH): a NON-prose extension under
    # docs/ must still block — the exemption is an allowlist of doc extensions,
    # NOT a denylist of a few known code extensions.
    @pytest.mark.parametrize(
        "path",
        [
            "docs/build.rs",       # Rust source
            "docs/config.yaml",    # CI/build config
            "docs/notebook.ipynb", # Jupyter (contains code)
            "docs/init.sql",       # SQL
            "docs/Script.java",    # Java
            "docs/deploy.ps1",     # PowerShell
            "docs/Makefile",       # extensionless build file (now fail-closed)
        ],
    )
    def test_inline_p1_non_prose_under_docs_still_blocks(self, guard_module, path):
        with self._mock(guard_module, [self._codex(1, _P1_BODY, path=path)]):
            block, _ = guard_module._check_inline_review_findings("100")
        assert block, f"{path!r} under docs/ is not prose — must block"

    def test_inline_p1_trailing_newline_path_blocks(self, guard_module):
        # Security review LOW: a trailing newline must not defeat the exemption.
        with self._mock(guard_module, [self._codex(1, _P1_BODY, path="docs/conf.py\n")]):
            block, _ = guard_module._check_inline_review_findings("100")
        assert block

    def test_inline_p1_on_license_no_ext_does_not_block(self, guard_module):
        with self._mock(guard_module, [self._codex(1, _P1_BODY, path="LICENSE")]):
            block, _ = guard_module._check_inline_review_findings("100")
        assert not block

    @pytest.mark.parametrize(
        "path",
        [
            "docs/requirements.txt",   # pip deps (repo classifies as config)
            "docs/constraints.txt",    # pip constraints
            "docs/CMakeLists.txt",     # CMake build
            "docs/meson_options.txt",  # Meson build
            "docs/guide.txt",          # plain .txt is ambiguous → fail-closed
        ],
    )
    def test_inline_p1_txt_manifest_or_ambiguous_under_docs_blocks(
        self, guard_module, path
    ):
        # Codex P1: `.txt` is NOT an unambiguous doc extension — a build/dep
        # manifest carries it too. Only a known doc STEM (LICENSE.txt) is prose.
        with self._mock(guard_module, [self._codex(1, _P1_BODY, path=path)]):
            block, _ = guard_module._check_inline_review_findings("100")
        assert block, f"{path!r} is an ambiguous .txt — must block"

    @pytest.mark.parametrize("path", ["LICENSE.txt", "README.txt", "docs/README.txt"])
    def test_inline_p1_known_stem_txt_does_not_block(self, guard_module, path):
        # A doc-named STEM pins the file as prose, so .txt is safe there.
        with self._mock(guard_module, [self._codex(1, _P1_BODY, path=path)]):
            block, _ = guard_module._check_inline_review_findings("100")
        assert not block

    @pytest.mark.parametrize(
        "path",
        [
            "docs/guide\x7f.md",  # DEL
            "docs/guide\x85.md",  # NEL (C1) — gh --jq emits literally
            "docs/guide\x9f.md",  # C1 upper bound
        ],
    )
    def test_inline_p1_c1_del_control_char_path_blocks(self, guard_module, path):
        # Codex P2: reject the COMPLETE control range (DEL + C1), not just C0.
        with self._mock(guard_module, [self._codex(1, _P1_BODY, path=path)]):
            block, _ = guard_module._check_inline_review_findings("100")
        assert block


class TestResolvePrNumber:
    """Fail-closed PR resolution: no-arg `gh pr merge` used to skip
    every merge gate (the gates only ran under `if pr_num:`)."""

    @pytest.mark.parametrize(
        ("cmd", "expected"),
        [
            ("gh pr merge 123 --squash", "123"),
            ("gh pr merge #77 --admin", "77"),
            ("gh pr merge https://github.com/o/r/pull/456 --squash", "456"),
            ('gh pr merge --subject "fix 123 things" 55', "55"),
        ],
    )
    def test_extract_variants(self, guard_module, cmd, expected):
        assert guard_module._extract_pr_number(cmd) == expected

    @pytest.mark.parametrize(
        ("cmd", "expected"),
        [
            # Digits in a CHAINED command must not stand in for the target
            # (`; echo 456` merges 123, not 456). 2026-07-10 review P1.
            ("gh pr merge --admin 123; echo 456", "123"),
            ("gh pr merge 123 && echo 999", "123"),
            ("gh pr merge --admin xyz; echo 456", None),  # no number → fall back
            ("gh pr merge 5 | tee 777", "5"),
            ("gh pr merge --admin xyz\necho 456", None),  # newline chain
            ("gh pr merge 123\necho 456", "123"),
            # A dangling value flag must NOT swallow the separator as its value
            # and then read a CHAINED command's digits (E3 guard: naive value-
            # skipping would return 999 here). 2026-08-19.
            ("gh pr merge --subject ; gh pr merge 999", None),
            ("gh pr merge -db ; gh pr merge 999", None),
        ],
    )
    def test_stops_at_shell_separator(self, guard_module, cmd, expected):
        assert guard_module._extract_pr_number(cmd) == expected

    def test_quoted_digits_not_a_pr_number(self, guard_module):
        # shlex keeps the quoted arg whole — '123' inside --subject must
        # not resolve as the PR number.
        cmd = 'gh pr merge --subject "fix 123"'
        assert guard_module._extract_pr_number(cmd) is None

    @pytest.mark.parametrize(
        ("cmd", "expected"),
        [
            # E3 (2026-08-19): an UNQUOTED numeric value that gh consumes as a
            # value-flag's argument must NOT be read as the PR number — gh takes
            # the trailing bare positional as the PR. `gh pr merge --subject 123 5`
            # merges PR 5 (subject "123"); the old loop returned 123 (the flag
            # value) → the gates checked the WRONG PR. Long, short, cluster,
            # and non-shadow (--match-head-commit / --repo) value flags all apply.
            ("gh pr merge --subject 123 5", "5"),
            ("gh pr merge -t 123 5", "5"),
            ("gh pr merge --body 123 5", "5"),
            ("gh pr merge --match-head-commit 123 5 --admin", "5"),
            ("gh pr merge --repo 123 5", "5"),
            ("gh pr merge -db 123 5", "5"),  # -d(bool)+-b(value): -b eats 123
        ],
    )
    def test_unquoted_flag_value_not_pr_number(self, guard_module, cmd, expected):
        assert guard_module._extract_pr_number(cmd) == expected

    @pytest.mark.parametrize(
        ("cmd", "expected"),
        [
            # GLUED value flags (sibling of E3, found in adversarial review): a
            # `--flag=value` long or a `-fvalue` short does NOT consume a next
            # token, so it falls through to the positional matchers — and the URL
            # matcher's `\S*` prefix would read a `/pull/N` smuggled INSIDE the
            # value as the PR while gh merges the trailing positional. A positional
            # PR ref never starts with '-'; the matchers must skip '-'-prefixed
            # tokens. gh merges 5 in every case here.
            ("gh pr merge --body=https://github.com/o/r/pull/999 5", "5"),
            ("gh pr merge -bhttps://github.com/o/r/pull/999 5", "5"),
            ("gh pr merge --body-file=/tmp/x/pull/42 5", "5"),
        ],
    )
    def test_glued_flag_value_url_not_pr_number(self, guard_module, cmd, expected):
        assert guard_module._extract_pr_number(cmd) == expected

    @pytest.mark.parametrize(
        ("cmd", "expected"),
        [
            # Regression LOCKS (must stay green): a positional BEFORE any flag is
            # still the PR; a GLUED long value flag never leaks its digits.
            ("gh pr merge 123 --subject 456", "123"),
            ("gh pr merge --subject=99 5", "5"),
            ("gh pr merge -db123 5", "5"),  # glued short value: -b's value is 123
            ("gh pr merge -- 5", "5"),  # -- end-of-options: the positional still resolves
        ],
    )
    def test_positional_pr_still_resolved(self, guard_module, cmd, expected):
        assert guard_module._extract_pr_number(cmd) == expected

    def test_resolves_current_branch_pr(self, guard_module):
        with patch.object(
            guard_module.subprocess,
            "run",
            return_value=subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="88\n",
                stderr="",
            ),
        ):
            assert guard_module._resolve_pr_number("gh pr merge --squash") == "88"

    def test_unresolvable_returns_none(self, guard_module):
        with patch.object(
            guard_module.subprocess,
            "run",
            return_value=subprocess.CompletedProcess(
                args=[],
                returncode=1,
                stdout="",
                stderr="no pr",
            ),
        ):
            assert guard_module._resolve_pr_number("gh pr merge --squash") is None

    def test_explicit_repo_numberless_fails_closed_without_gh(self, guard_module):
        # explicit user --repo + no number + no cwd → None WITHOUT touching gh
        # (gh pr view --repo errors without a selector; resolving a cwd-branch PR
        # and gating it against a DIFFERENT repo is the wrong-PR bug).
        with patch.object(guard_module.subprocess, "run", side_effect=AssertionError):
            assert guard_module._resolve_pr_number("gh pr merge --squash", repo="o/r") is None

    def test_derived_cwd_numberless_resolves_without_repo_flag(self, guard_module):
        # A merge whose repo was DERIVED from cwd (F3): the numberless branch PR
        # resolves by running gh IN that cwd with NO --repo — so it doesn't hit
        # the `--repo needs a selector` error the previous line guards against.
        with patch.object(
            guard_module.subprocess,
            "run",
            return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout="88\n"),
        ) as run:
            got = guard_module._resolve_pr_number("gh pr merge --squash", repo="o/r", cwd="/wt")
        assert got == "88"
        argv = run.call_args_list[0].args[0]
        assert "--repo" not in argv
        assert run.call_args_list[0].kwargs.get("cwd") == "/wt"


# ── CI-status merge gate (_pr_ci_status / _has_ci_override) ───────────────


class TestPrCiStatus:
    """The CI classifier that blocks red/pending merges (env-injected, no net)."""

    @staticmethod
    def _set(monkeypatch, checks):
        monkeypatch.setenv("_TEST_GH_CI_ROLLUP", json.dumps(checks))

    def test_all_success_is_green(self, guard_module, monkeypatch):
        self._set(
            monkeypatch,
            [
                {"name": "test", "workflowName": "CI", "status": "COMPLETED", "conclusion": "SUCCESS"},
                {"name": "lint", "workflowName": "CI", "status": "COMPLETED", "conclusion": "SUCCESS"},
            ],
        )
        assert guard_module._pr_ci_status("1") == ("green", [])

    def test_failure_is_red(self, guard_module, monkeypatch):
        self._set(
            monkeypatch,
            [
                {"name": "test", "status": "COMPLETED", "conclusion": "FAILURE"},
                {"name": "lint", "status": "COMPLETED", "conclusion": "SUCCESS"},
            ],
        )
        assert guard_module._pr_ci_status("1") == ("red", ["test"])

    def test_in_progress_is_pending(self, guard_module, monkeypatch):
        self._set(
            monkeypatch,
            [
                {"name": "test", "status": "IN_PROGRESS", "conclusion": None},
                {"name": "lint", "status": "COMPLETED", "conclusion": "SUCCESS"},
            ],
        )
        assert guard_module._pr_ci_status("1") == ("pending", ["test"])

    def test_red_beats_pending(self, guard_module, monkeypatch):
        self._set(
            monkeypatch,
            [
                {"name": "test", "conclusion": "FAILURE"},
                {"name": "build", "status": "IN_PROGRESS"},
            ],
        )
        assert guard_module._pr_ci_status("1")[0] == "red"

    def test_skipped_and_neutral_ignored(self, guard_module, monkeypatch):
        self._set(
            monkeypatch,
            [
                {"name": "test", "workflowName": "CI", "conclusion": "SUCCESS"},
                {"name": "opt", "conclusion": "SKIPPED"},
                {"name": "info", "conclusion": "NEUTRAL"},
            ],
        )
        assert guard_module._pr_ci_status("1") == ("green", [])

    def test_legacy_statuscontext_failure_is_red(self, guard_module, monkeypatch):
        self._set(monkeypatch, [{"context": "legacy-ci", "state": "FAILURE"}])
        assert guard_module._pr_ci_status("1") == ("red", ["legacy-ci"])

    def test_empty_rollup_is_absent(self, guard_module, monkeypatch):
        # A READABLE, genuinely-empty rollup ("[]") = zero checks = CI has not run.
        # This is DISTINCT from "unknown" (could not read): it is a definite fact,
        # so it is "absent" — the canonical-repo merge arm fail-CLOSES on it.
        monkeypatch.setenv("_TEST_GH_CI_ROLLUP", "[]")
        assert guard_module._pr_ci_status("1") == ("absent", [])

    def test_no_output_is_unknown(self, guard_module, monkeypatch):
        # An EMPTY string (gh produced no output) is NOT a readable empty list — we
        # cannot tell zero-checks from a silent read failure, so it stays fail-open
        # "unknown", never "absent".
        monkeypatch.setenv("_TEST_GH_CI_ROLLUP", "")
        assert guard_module._pr_ci_status("1") == ("unknown", [])

    def test_garbage_is_unknown(self, guard_module, monkeypatch):
        monkeypatch.setenv("_TEST_GH_CI_ROLLUP", "not-json")
        assert guard_module._pr_ci_status("1") == ("unknown", [])

    def test_non_ci_payload_is_unknown_fail_open(self, guard_module, monkeypatch):
        # e.g. a review-comment mock other tests inject — must not be read as red.
        # A NON-empty payload with no CI-shaped entries stays "unknown" (we saw
        # something but recognized no CI verdict) — NOT "absent" (which is zero checks).
        self._set(monkeypatch, [{"login": "codex", "body": "FAILURE somewhere"}])
        assert guard_module._pr_ci_status("1") == ("unknown", [])


class TestPrCiStatusRequiredWorkflows:
    """Required-CI-workflow identity (closes the #1484 P2 partial-rollup residual):
    a NON-empty rollup whose present checks are all green must still assert that every
    REQUIRED workflow (rollup ``workflowName``, config-driven, default ``CI``) actually
    contributed a verdict — otherwise a workflow-specific trigger drop (e.g. only a
    green CodeQL, the CI suite absent) reads green and merges an untested PR. Missing
    required workflow(s) → the new ``"incomplete"`` state, carrying the missing names."""

    @staticmethod
    def _set(monkeypatch, checks):
        monkeypatch.setenv("_TEST_GH_CI_ROLLUP", json.dumps(checks))

    def test_codeql_only_is_incomplete(self, guard_module, monkeypatch):
        # THE Codex-P2 payload: a lone green CodeQL, the CI suite entirely absent.
        self._set(monkeypatch, [
            {"name": "Analyze (python)", "workflowName": "CodeQL", "status": "COMPLETED", "conclusion": "SUCCESS"},
        ])
        assert guard_module._pr_ci_status("1") == ("incomplete", ["CI"])

    def test_red_beats_incomplete(self, guard_module, monkeypatch):
        # A red check blocks as red even when the required workflow is ALSO missing —
        # red carries the more actionable verdict (and blocks regardless).
        self._set(monkeypatch, [
            {"name": "Analyze (python)", "workflowName": "CodeQL", "status": "COMPLETED", "conclusion": "FAILURE"},
        ])
        assert guard_module._pr_ci_status("1") == ("red", ["Analyze (python)"])

    def test_pending_beats_incomplete(self, guard_module, monkeypatch):
        self._set(monkeypatch, [
            {"name": "Analyze (python)", "workflowName": "CodeQL", "status": "IN_PROGRESS", "conclusion": None},
        ])
        assert guard_module._pr_ci_status("1") == ("pending", ["Analyze (python)"])

    def test_required_present_only_as_skipped_is_incomplete(self, guard_module, monkeypatch):
        # A required suite whose every entry SKIPPED tested nothing — skipped entries
        # do not count as "ran". (On the canonical repo ci.yml has no paths filters,
        # so a fully-skipped suite is anomalous, and # ci-override is the escape.)
        self._set(monkeypatch, [
            {"name": "test", "workflowName": "CI", "status": "COMPLETED", "conclusion": "SKIPPED"},
            {"name": "Analyze (python)", "workflowName": "CodeQL", "status": "COMPLETED", "conclusion": "SUCCESS"},
        ])
        assert guard_module._pr_ci_status("1") == ("incomplete", ["CI"])

    def test_statuscontext_only_is_incomplete(self, guard_module, monkeypatch):
        # Legacy StatusContexts have no workflowName and can never satisfy a required
        # workflow identity — fail-closed on the canonical (Actions-only) repo.
        self._set(monkeypatch, [{"context": "legacy-ci", "state": "SUCCESS"}])
        assert guard_module._pr_ci_status("1") == ("incomplete", ["CI"])

    def test_statuscontext_green_with_workflowname_does_not_vouch(
        self, guard_module, monkeypatch
    ):
        # Hardening (architect NOTE): a StatusContext-shaped green (state=SUCCESS)
        # gluing on a workflowName key must NOT satisfy the required identity — only
        # a CheckRun-shaped pass (conclusion=SUCCESS) vouches, by construction, not
        # merely because gh's current schema lacks the field on StatusContexts.
        self._set(monkeypatch, [{"context": "x", "state": "SUCCESS", "workflowName": "CI"}])
        assert guard_module._pr_ci_status("1") == ("incomplete", ["CI"])

    def test_match_is_case_insensitive(self, guard_module, monkeypatch):
        monkeypatch.setenv("_TEST_REQUIRED_CI_WORKFLOWS", "ci")
        self._set(monkeypatch, [
            {"name": "test", "workflowName": "CI", "status": "COMPLETED", "conclusion": "SUCCESS"},
        ])
        assert guard_module._pr_ci_status("1") == ("green", [])

    def test_multi_required_one_missing_names_it(self, guard_module, monkeypatch):
        monkeypatch.setenv("_TEST_REQUIRED_CI_WORKFLOWS", "CI,CodeQL")
        self._set(monkeypatch, [
            {"name": "test", "workflowName": "CI", "status": "COMPLETED", "conclusion": "SUCCESS"},
        ])
        assert guard_module._pr_ci_status("1") == ("incomplete", ["CodeQL"])

    def test_multi_required_all_present_is_green(self, guard_module, monkeypatch):
        monkeypatch.setenv("_TEST_REQUIRED_CI_WORKFLOWS", "CI,CodeQL")
        self._set(monkeypatch, [
            {"name": "test", "workflowName": "CI", "status": "COMPLETED", "conclusion": "SUCCESS"},
            {"name": "Analyze (python)", "workflowName": "CodeQL", "status": "COMPLETED", "conclusion": "SUCCESS"},
        ])
        assert guard_module._pr_ci_status("1") == ("green", [])

    def test_blank_seam_falls_back_to_default(self, guard_module, monkeypatch):
        # "" parses to an empty list = invalid = fail CLOSED to the default ("CI").
        # There is deliberately NO disable value — an empty required set would turn
        # the identity check off entirely.
        monkeypatch.setenv("_TEST_REQUIRED_CI_WORKFLOWS", "")
        self._set(monkeypatch, [
            {"name": "Analyze (python)", "workflowName": "CodeQL", "status": "COMPLETED", "conclusion": "SUCCESS"},
        ])
        assert guard_module._pr_ci_status("1") == ("incomplete", ["CI"])

    def test_absent_still_wins_over_incomplete(self, guard_module, monkeypatch):
        # An EMPTY rollup is "absent" (zero checks), never "incomplete" (partial).
        monkeypatch.setenv("_TEST_GH_CI_ROLLUP", "[]")
        assert guard_module._pr_ci_status("1") == ("absent", [])


class TestRequiredCiWorkflowsConfig:
    """_required_ci_workflows(): config-driven required set, fail-CLOSED to the
    default ("CI") on ANY malformed/ambiguous input — mirroring
    _required_scheduled_review_kinds. These tests exercise the real file path, so
    they delete the seam and point HOME at a tmp dir."""

    @staticmethod
    def _with_yaml(monkeypatch, tmp_path, text: str | None):
        monkeypatch.delenv("_TEST_REQUIRED_CI_WORKFLOWS", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        if text is not None:
            cfg = tmp_path / ".genesis" / "config"
            cfg.mkdir(parents=True)
            (cfg / "genesis.yaml").write_text(text)

    def test_valid_config_replaces_default(self, guard_module, monkeypatch, tmp_path):
        self._with_yaml(monkeypatch, tmp_path, "merge_gate:\n  required_ci_workflows: [My Suite]\n")
        assert guard_module._required_ci_workflows() == ("My Suite",)

    def test_multiple_deduped_order_preserved(self, guard_module, monkeypatch, tmp_path):
        self._with_yaml(
            monkeypatch, tmp_path,
            "merge_gate:\n  required_ci_workflows: [CodeQL, CI, CodeQL]\n",
        )
        assert guard_module._required_ci_workflows() == ("CodeQL", "CI")

    def test_missing_file_falls_back_to_default(self, guard_module, monkeypatch, tmp_path):
        self._with_yaml(monkeypatch, tmp_path, None)
        assert guard_module._required_ci_workflows() == ("CI",)

    def test_missing_key_falls_back_to_default(self, guard_module, monkeypatch, tmp_path):
        self._with_yaml(monkeypatch, tmp_path, "merge_gate:\n  required_scheduled_reviews: [leaks]\n")
        assert guard_module._required_ci_workflows() == ("CI",)

    def test_empty_list_is_invalid_no_disable(self, guard_module, monkeypatch, tmp_path):
        # [] would DISABLE the identity check — fail closed to the default instead.
        self._with_yaml(monkeypatch, tmp_path, "merge_gate:\n  required_ci_workflows: []\n")
        assert guard_module._required_ci_workflows() == ("CI",)

    def test_non_list_is_invalid(self, guard_module, monkeypatch, tmp_path):
        self._with_yaml(monkeypatch, tmp_path, "merge_gate:\n  required_ci_workflows: CI\n")
        assert guard_module._required_ci_workflows() == ("CI",)

    def test_non_string_element_is_invalid(self, guard_module, monkeypatch, tmp_path):
        self._with_yaml(monkeypatch, tmp_path, "merge_gate:\n  required_ci_workflows: [123]\n")
        assert guard_module._required_ci_workflows() == ("CI",)

    def test_blank_element_is_invalid(self, guard_module, monkeypatch, tmp_path):
        self._with_yaml(monkeypatch, tmp_path, 'merge_gate:\n  required_ci_workflows: [" "]\n')
        assert guard_module._required_ci_workflows() == ("CI",)

    def test_duplicate_merge_gate_key_is_invalid(self, guard_module, monkeypatch, tmp_path):
        # yaml.safe_load keeps the LAST duplicate silently — the line-scan guard must
        # reject the ambiguous file rather than honor whichever copy wins.
        self._with_yaml(
            monkeypatch, tmp_path,
            "merge_gate:\n  required_ci_workflows: [CI]\n"
            "merge_gate:\n  required_ci_workflows: [Decoy]\n",
        )
        # yaml would honor the LAST copy ([Decoy]); fail-closed rejects the whole
        # ambiguous file and returns the DEFAULT instead — never the decoy.
        assert guard_module._required_ci_workflows() == ("CI",)

    def test_duplicate_workflows_key_is_invalid(self, guard_module, monkeypatch, tmp_path):
        self._with_yaml(
            monkeypatch, tmp_path,
            "merge_gate:\n  required_ci_workflows: [CI]\n  required_ci_workflows: [Decoy]\n",
        )
        assert guard_module._required_ci_workflows() == ("CI",)

    def test_seam_overrides_file(self, guard_module, monkeypatch, tmp_path):
        self._with_yaml(monkeypatch, tmp_path, "merge_gate:\n  required_ci_workflows: [FileSuite]\n")
        monkeypatch.setenv("_TEST_REQUIRED_CI_WORKFLOWS", "SeamSuite")
        assert guard_module._required_ci_workflows() == ("SeamSuite",)

    def test_discarded_declared_policy_prints_note(self, guard_module, monkeypatch, tmp_path, capsys):
        # Architect SHOULD-FIX: free-text config can EXPAND the required set, so a
        # silent fallback-to-default would NARROW a declared stricter policy with no
        # signal. A present-but-invalid key must say so on stderr.
        self._with_yaml(monkeypatch, tmp_path, "merge_gate:\n  required_ci_workflows: []\n")
        assert guard_module._required_ci_workflows() == ("CI",)
        assert "required_ci_workflows" in capsys.readouterr().err

    def test_key_absent_is_silent(self, guard_module, monkeypatch, tmp_path, capsys):
        # The normal install (no key configured) must NOT warn — the default is the
        # intended policy, not a substitution.
        self._with_yaml(monkeypatch, tmp_path, "merge_gate:\n  required_scheduled_reviews: [leaks]\n")
        assert guard_module._required_ci_workflows() == ("CI",)
        assert capsys.readouterr().err == ""

    def test_duplicate_key_prints_note(self, guard_module, monkeypatch, tmp_path, capsys):
        self._with_yaml(
            monkeypatch, tmp_path,
            "merge_gate:\n  required_ci_workflows: [CI]\n  required_ci_workflows: [Decoy]\n",
        )
        assert guard_module._required_ci_workflows() == ("CI",)
        assert "required_ci_workflows" in capsys.readouterr().err


class TestPrCiStatusCancelSibling:
    """Concurrency-cancel dedup (approach B): a CANCELLED CheckRun is dropped IFF a
    check of the SAME identity (name + workflowName) concluded SUCCESS AT OR AFTER
    it — so a superseded `cancel-in-progress` duplicate (cancel older than its
    re-run's success) drops, but a SUCCESS-then-cancel re-run on an unchanged head
    still blocks. Both sides are terminal COMPLETED runs (always carry completedAt);
    every unresolvable case (no identity, no completedAt, no later success) fails
    closed to red."""

    @staticmethod
    def _set(monkeypatch, checks):
        monkeypatch.setenv("_TEST_GH_CI_ROLLUP", json.dumps(checks))

    def test_cancel_with_success_sibling_is_green(self, guard_module, monkeypatch):
        # The ec925917 incident: concurrency-cancelled CodeQL dups (older) + their
        # successful re-runs (newer, same name + workflowName). Must read green.
        # (Required-identity policy pinned to the fixture's actual workflow so the
        # incident payload stays byte-faithful — CodeQL-only was the real rollup.)
        monkeypatch.setenv("_TEST_REQUIRED_CI_WORKFLOWS", "CodeQL")
        self._set(monkeypatch, [
            {"name": "Analyze (python)", "workflowName": "CodeQL", "status": "COMPLETED", "conclusion": "CANCELLED", "completedAt": "2026-08-21T10:00:00Z"},
            {"name": "Analyze (python)", "workflowName": "CodeQL", "status": "COMPLETED", "conclusion": "SUCCESS", "completedAt": "2026-08-21T10:05:00Z"},
            {"name": "Analyze (actions)", "workflowName": "CodeQL", "status": "COMPLETED", "conclusion": "CANCELLED", "completedAt": "2026-08-21T10:00:00Z"},
            {"name": "Analyze (actions)", "workflowName": "CodeQL", "status": "COMPLETED", "conclusion": "SUCCESS", "completedAt": "2026-08-21T10:05:00Z"},
        ])
        assert guard_module._pr_ci_status("1") == ("green", [])

    def test_cancel_without_success_sibling_stays_red(self, guard_module, monkeypatch):
        # A genuine cancel with no same-identity success still blocks the merge.
        self._set(monkeypatch, [
            {"name": "Analyze", "workflowName": "CodeQL", "status": "COMPLETED", "conclusion": "CANCELLED"},
            {"name": "lint", "workflowName": "CI", "status": "COMPLETED", "conclusion": "SUCCESS"},
        ])
        assert guard_module._pr_ci_status("1") == ("red", ["Analyze"])

    def test_cross_workflow_same_name_does_not_drop(self, guard_module, monkeypatch):
        # SECURITY (HIGH lock): a same-NAME success from a DIFFERENT workflow is
        # NOT a valid sibling and must not mask a genuinely-cancelled required
        # check. Identity is (name, workflowName), so this stays red. Under a
        # bare-name match this would wrongly go green.
        self._set(monkeypatch, [
            {"name": "test-suite", "workflowName": "real-ci", "status": "COMPLETED", "conclusion": "CANCELLED", "completedAt": "2026-08-21T10:00:00Z"},
            {"name": "test-suite", "workflowName": "decoy", "status": "COMPLETED", "conclusion": "SUCCESS", "completedAt": "2026-08-21T10:05:00Z"},
        ])
        assert guard_module._pr_ci_status("1") == ("red", ["test-suite"])

    def test_cancel_without_workflowname_stays_red(self, guard_module, monkeypatch):
        # Fail-closed: a CANCELLED entry with no resolvable workflowName has no
        # identity, so even a later same-name SUCCESS cannot drop it — stays red.
        self._set(monkeypatch, [
            {"name": "x", "status": "COMPLETED", "conclusion": "CANCELLED", "completedAt": "2026-08-21T10:00:00Z"},
            {"name": "x", "status": "COMPLETED", "conclusion": "SUCCESS", "completedAt": "2026-08-21T10:05:00Z"},
        ])
        assert guard_module._pr_ci_status("1") == ("red", ["x"])

    def test_cancel_plus_inflight_rerun_never_green(self, guard_module, monkeypatch):
        # Cancel + an in-flight re-run of the same identity, no success yet → must
        # NOT read green (the re-run is still running; nothing has passed).
        self._set(monkeypatch, [
            {"name": "Analyze", "workflowName": "CodeQL", "status": "COMPLETED", "conclusion": "CANCELLED"},
            {"name": "Analyze", "workflowName": "CodeQL", "status": "IN_PROGRESS", "conclusion": None},
        ])
        assert guard_module._pr_ci_status("1")[0] != "green"

    def test_cancel_sibling_does_not_launder_other_failure(self, guard_module, monkeypatch):
        # Dropping a cancelled-with-sibling must never hide a DIFFERENT check's
        # real failure — the core wrong-green guard.
        self._set(monkeypatch, [
            {"name": "test", "workflowName": "CI", "status": "COMPLETED", "conclusion": "CANCELLED", "completedAt": "2026-08-21T10:00:00Z"},
            {"name": "test", "workflowName": "CI", "status": "COMPLETED", "conclusion": "SUCCESS", "completedAt": "2026-08-21T10:05:00Z"},
            {"name": "lint", "workflowName": "CI", "status": "COMPLETED", "conclusion": "FAILURE"},
        ])
        assert guard_module._pr_ci_status("1") == ("red", ["lint"])

    def test_timed_out_with_success_sibling_still_red(self, guard_module, monkeypatch):
        # Scope lock: ONLY CANCELLED is laundered. TIMED_OUT carries a real
        # verdict and still blocks even with a same-identity success.
        self._set(monkeypatch, [
            {"name": "test", "workflowName": "CI", "status": "COMPLETED", "conclusion": "TIMED_OUT"},
            {"name": "test", "workflowName": "CI", "status": "COMPLETED", "conclusion": "SUCCESS"},
        ])
        assert guard_module._pr_ci_status("1") == ("red", ["test"])

    def test_statuscontext_success_does_not_drop_checkrun_cancel(self, guard_module, monkeypatch):
        # A legacy StatusContext SUCCESS (no workflowName) has no CheckRun identity
        # and is NOT a valid sibling for a CheckRun CANCELLED of the same name —
        # the cancel stays red (closes the cross-shape collision surface).
        self._set(monkeypatch, [
            {"context": "ci/legacy", "state": "SUCCESS"},
            {"name": "ci/legacy", "workflowName": "CI", "status": "COMPLETED", "conclusion": "CANCELLED"},
        ])
        assert guard_module._pr_ci_status("1") == ("red", ["ci/legacy"])

    def test_pending_preserved_when_sibling_dropped(self, guard_module, monkeypatch):
        # Dropping a cancelled-with-sibling must not swallow an UNRELATED pending
        # check — the merge still blocks on the in-flight one.
        self._set(monkeypatch, [
            {"name": "test", "workflowName": "CI", "status": "COMPLETED", "conclusion": "CANCELLED", "completedAt": "2026-08-21T10:00:00Z"},
            {"name": "test", "workflowName": "CI", "status": "COMPLETED", "conclusion": "SUCCESS", "completedAt": "2026-08-21T10:05:00Z"},
            {"name": "deploy", "workflowName": "CI", "status": "IN_PROGRESS", "conclusion": None},
        ])
        assert guard_module._pr_ci_status("1") == ("pending", ["deploy"])

    def test_nameless_cancel_not_dropped_by_nameless_success(self, guard_module, monkeypatch):
        # Entries with no name/workflowName have no identity (_ci_identity → None)
        # and are excluded from the sibling set — a nameless SUCCESS can't license
        # dropping a nameless CANCELLED. Stays red (fail-safe over-block).
        self._set(monkeypatch, [
            {"status": "COMPLETED", "conclusion": "SUCCESS"},
            {"status": "COMPLETED", "conclusion": "CANCELLED"},
        ])
        assert guard_module._pr_ci_status("1") == ("red", ["check"])

    def test_success_then_cancel_on_unchanged_head_stays_red(self, guard_module, monkeypatch):
        # Codex P1: a job passed (older), then was re-run on the UNCHANGED head and
        # that re-run was CANCELLED (newer). The latest attempt never passed, so the
        # cancel is NOT superseded by a later success → stays red. Under a bare
        # set-membership match (no completedAt) this wrongly returned green.
        self._set(monkeypatch, [
            {"name": "test", "workflowName": "CI", "status": "COMPLETED", "conclusion": "SUCCESS", "completedAt": "2026-08-21T10:00:00Z"},
            {"name": "test", "workflowName": "CI", "status": "COMPLETED", "conclusion": "CANCELLED", "completedAt": "2026-08-21T10:05:00Z"},
        ])
        assert guard_module._pr_ci_status("1") == ("red", ["test"])

    def test_cancel_without_completedat_stays_red(self, guard_module, monkeypatch):
        # Fail-closed: a CANCELLED entry with no completedAt can't be proven
        # superseded (no timestamp to order it), so even a same-identity SUCCESS
        # does not drop it — stays red.
        self._set(monkeypatch, [
            {"name": "test", "workflowName": "CI", "status": "COMPLETED", "conclusion": "SUCCESS", "completedAt": "2026-08-21T10:05:00Z"},
            {"name": "test", "workflowName": "CI", "status": "COMPLETED", "conclusion": "CANCELLED"},
        ])
        assert guard_module._pr_ci_status("1") == ("red", ["test"])


class TestCiOverrideSigil:
    """The `# ci-override` sigil is quote-aware and (at the call site) bound to
    the merge segment — it cannot be spoofed from a quoted --body or a chained
    command. This is the BLOCKER the reviewer caught in the first draft."""

    def test_trailing_comment_accepted(self, guard_module):
        assert has_trailing_override("gh pr merge 5 --squash --admin  # ci-override", "ci-override")

    def test_absent(self, guard_module):
        assert not has_trailing_override("gh pr merge 5 --squash --admin", "ci-override")

    def test_review_override_is_not_ci_override(self, guard_module):
        assert not has_trailing_override(
            "gh pr merge 5 --squash --admin  # review-override", "ci-override"
        )

    def test_sigil_in_quoted_body_rejected(self, guard_module):
        # No real '#' comment — sigil is just text inside --body.
        assert not has_trailing_override(
            'gh pr merge 5 --admin --body "flaky, see ci-override note"', "ci-override"
        )

    def test_hash_sigil_inside_quotes_rejected(self, guard_module):
        # A literal '# ci-override' buried INSIDE quotes must not count.
        assert not has_trailing_override(
            'gh pr merge 5 --admin --body "x # ci-override"', "ci-override"
        )

    def test_not_bound_to_other_segment(self, guard_module):
        # Sigil on a chained commit segment must not waive the merge segment.
        segs = analyze("git commit -m wip  # ci-override && gh pr merge 5 --squash --admin")
        merge = [s for s in segs if gh_pr_subcommand(s.argv) == "merge"][0]
        assert not has_trailing_override(merge.raw, "ci-override")

    def test_default_sigil_review_override_unchanged(self, guard_module):
        assert has_trailing_override("gh pr merge 5 --admin  # review-override")


class TestCiGateEndToEnd:
    """The gate actually returns exit 2 through main() on red CI — closes the
    'mechanism present but not reached' gap the reviewer flagged."""

    def _payload(self, cmd):
        return {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": cmd},
        }

    def _patch_merge_ok(self, guard_module, monkeypatch):
        # The gate functions gained a `repo` kwarg (cross-repo --repo threading);
        # the fakes accept it (and ignore it) so the signatures still match.
        monkeypatch.setattr(guard_module, "_check_mergeable", lambda n, repo=None: "MERGEABLE")
        monkeypatch.setattr(
            guard_module,
            "_check_pr_review_findings",
            lambda n, force=False, repo=None: (False, ""),
        )
        monkeypatch.setattr(
            guard_module,
            "_check_inline_review_findings",
            lambda n, force=False, repo=None: (False, ""),
        )
        # The Codex review-freshness gate (PR #1366) is a real network check —
        # mock it pass-through (None verified_head also disengages the
        # --match-head-commit binding) so this suite stays offline-hermetic.
        monkeypatch.setattr(
            guard_module,
            "_check_codex_reviewed_head",
            lambda n, force=False, repo=None: (False, "", None),
        )
        # Base-branch invariant (PR #1366) also makes real gh calls — mock it
        # pass-through so this suite stays offline-hermetic.
        monkeypatch.setattr(
            guard_module,
            "_check_base_is_default",
            lambda n, force=False, repo=None: (False, ""),
        )
        # Scheduled-Claude-review gate is a real network check too — pass-through.
        monkeypatch.setattr(
            guard_module,
            "_check_scheduled_claude_reviewed_head",
            lambda n, head=None, repo=None, force=False, strict=False: None,
        )
        # The pin-receipt gate is downstream too. Without this it runs for real
        # against the cwd repo's PR — whose tree predates scripts/lib/cc_version.sh,
        # so the read is a genuine 404 and the gate correctly BLOCKS, which has
        # nothing to do with the invariant under test here.
        monkeypatch.setattr(guard_module, "_check_pin_receipts", lambda n, repo=None: (False, ""))

    def test_red_ci_blocks_exit_2(self, guard_module, monkeypatch, capsys):
        monkeypatch.setenv(
            "_TEST_GH_CI_ROLLUP", json.dumps([{"name": "test", "conclusion": "FAILURE"}])
        )
        self._patch_merge_ok(guard_module, monkeypatch)
        monkeypatch.setattr(
            guard_module,
            "read_payload",
            lambda: self._payload("gh pr merge 5 --squash --admin"),
        )
        rc = guard_module.main()
        assert rc == 2
        assert "CI is RED" in capsys.readouterr().err

    def test_red_ci_with_override_allowed(self, guard_module, monkeypatch):
        monkeypatch.setenv(
            "_TEST_GH_CI_ROLLUP", json.dumps([{"name": "test", "conclusion": "FAILURE"}])
        )
        self._patch_merge_ok(guard_module, monkeypatch)
        monkeypatch.setattr(
            guard_module,
            "read_payload",
            lambda: self._payload("gh pr merge 5 --squash --admin  # ci-override"),
        )
        assert guard_module.main() == 0

    def test_pending_ci_blocks(self, guard_module, monkeypatch):
        monkeypatch.setenv(
            "_TEST_GH_CI_ROLLUP",
            json.dumps([{"name": "test", "status": "IN_PROGRESS"}]),
        )
        self._patch_merge_ok(guard_module, monkeypatch)
        monkeypatch.setattr(
            guard_module,
            "read_payload",
            lambda: self._payload("gh pr merge 5 --squash --admin"),
        )
        assert guard_module.main() == 2

    def test_green_ci_allowed(self, guard_module, monkeypatch):
        monkeypatch.setenv(
            "_TEST_GH_CI_ROLLUP",
            json.dumps([{"name": "test", "workflowName": "CI", "conclusion": "SUCCESS"}]),
        )
        self._patch_merge_ok(guard_module, monkeypatch)
        monkeypatch.setattr(
            guard_module,
            "read_payload",
            lambda: self._payload("gh pr merge 5 --squash --admin"),
        )
        assert guard_module.main() == 0

    def test_incomplete_ci_blocks_exit_2(self, guard_module, monkeypatch, capsys):
        # The #1484-P2 payload end-to-end: a lone green CodeQL (CI suite missing)
        # must BLOCK the merge, naming the missing required workflow. Canonical
        # scoping engages because an unresolved merge repo fails CLOSED.
        monkeypatch.setenv(
            "_TEST_GH_CI_ROLLUP",
            json.dumps([
                {"name": "Analyze (python)", "workflowName": "CodeQL", "status": "COMPLETED", "conclusion": "SUCCESS"},
            ]),
        )
        self._patch_merge_ok(guard_module, monkeypatch)
        monkeypatch.setattr(
            guard_module,
            "read_payload",
            lambda: self._payload("gh pr merge 5 --squash --admin"),
        )
        rc = guard_module.main()
        err = capsys.readouterr().err
        assert rc == 2
        assert "required CI workflow" in err
        assert "CI" in err  # the missing workflow is named

    def test_incomplete_ci_with_override_allowed(self, guard_module, monkeypatch, capsys):
        monkeypatch.setenv(
            "_TEST_GH_CI_ROLLUP",
            json.dumps([
                {"name": "Analyze (python)", "workflowName": "CodeQL", "status": "COMPLETED", "conclusion": "SUCCESS"},
            ]),
        )
        self._patch_merge_ok(guard_module, monkeypatch)
        monkeypatch.setattr(
            guard_module,
            "read_payload",
            lambda: self._payload("gh pr merge 5 --squash --admin  # ci-override"),
        )
        assert guard_module.main() == 0
        assert "consciously accepted" in capsys.readouterr().err

    def test_incomplete_ci_off_canonical_fails_open(self, guard_module, monkeypatch):
        # Off the canonical repo the identity check must NOT block — another repo's
        # CI may legitimately be named differently (or not exist at all).
        monkeypatch.setenv("_TEST_CANONICAL_PUBLIC_REPO", "owner/canonical-repo")
        monkeypatch.setenv(
            "_TEST_GH_CI_ROLLUP",
            json.dumps([
                {"name": "Analyze (python)", "workflowName": "CodeQL", "status": "COMPLETED", "conclusion": "SUCCESS"},
            ]),
        )
        self._patch_merge_ok(guard_module, monkeypatch)
        monkeypatch.setattr(
            guard_module,
            "read_payload",
            lambda: self._payload(
                "gh pr merge 5 --repo octo/other-repo --squash --admin"
            ),
        )
        assert guard_module.main() == 0


class TestCiStatusUnfinishedStates:
    """Full enumeration of GitHub's status/conclusion/state domains — no
    unenumerated value may read green: ANY non-terminal status ⇒ pending; an
    unrecognized *terminal* conclusion (e.g. STALE, or a value GitHub adds later)
    ⇒ red (fail-closed); a legacy EXPECTED context ⇒ pending. A null conclusion on
    a COMPLETED run is genuine no-verdict data and stays a benign ignore."""

    @staticmethod
    def _set(monkeypatch, checks):
        monkeypatch.setenv("_TEST_GH_CI_ROLLUP", json.dumps(checks))

    def test_pending_status_is_pending(self, guard_module, monkeypatch):
        self._set(monkeypatch, [
            {"name": "test", "status": "PENDING"},
            {"name": "lint", "status": "COMPLETED", "conclusion": "SUCCESS"},
        ])
        assert guard_module._pr_ci_status("1") == ("pending", ["test"])

    def test_waiting_requested_new_states_are_pending(self, guard_module, monkeypatch):
        for st in ("WAITING", "REQUESTED", "SOME_NEW_STATE"):
            self._set(monkeypatch, [{"name": "test", "status": st}])
            assert guard_module._pr_ci_status("1")[0] == "pending", st

    def test_completed_null_conclusion_not_pending(self, guard_module, monkeypatch):
        self._set(monkeypatch, [
            {"name": "test", "status": "COMPLETED", "conclusion": None},
            {"name": "lint", "workflowName": "CI", "status": "COMPLETED", "conclusion": "SUCCESS"},
        ])
        assert guard_module._pr_ci_status("1") == ("green", [])

    def test_stale_conclusion_is_red(self, guard_module, monkeypatch):
        # STALE = a superseded/outdated run (GitHub's own term) — NOT a pass. On a
        # gate that forces --admin (sole CI enforcement) it must block, not read
        # green. Under the prior code this silently ignored → green.
        self._set(monkeypatch, [
            {"name": "Analyze (python)", "workflowName": "CodeQL", "status": "COMPLETED", "conclusion": "STALE"},
        ])
        assert guard_module._pr_ci_status("1") == ("red", ["Analyze (python)"])

    def test_stale_mixed_with_success_still_red(self, guard_module, monkeypatch):
        self._set(monkeypatch, [
            {"name": "codeql", "status": "COMPLETED", "conclusion": "STALE"},
            {"name": "test", "status": "COMPLETED", "conclusion": "SUCCESS"},
        ])
        assert guard_module._pr_ci_status("1") == ("red", ["codeql"])

    def test_unrecognized_terminal_conclusion_fails_closed_red(self, guard_module, monkeypatch):
        # A COMPLETED run with a conclusion GitHub adds later must fail closed to
        # red, never silently ignore (the class root cause behind the STALE gap).
        self._set(monkeypatch, [{"name": "test", "status": "COMPLETED", "conclusion": "SOME_FUTURE_VERDICT"}])
        assert guard_module._pr_ci_status("1") == ("red", ["test"])

    def test_expected_statuscontext_is_pending(self, guard_module, monkeypatch):
        # A legacy StatusContext EXPECTED (a required context that hasn't reported
        # yet) must read pending, not fall through to unknown (which callers treat
        # as non-blocking).
        self._set(monkeypatch, [{"context": "ci/required", "state": "EXPECTED"}])
        assert guard_module._pr_ci_status("1") == ("pending", ["ci/required"])


class TestMergeableAllowlist:
    """F2: the mergeability gate is an ALLOWLIST — block unless status is a
    definite MERGEABLE. The old `== UNKNOWN` check let None/'' (a FAILED query;
    _check_mergeable fails OPEN to None) sail through and merge. Both the
    enforcement arm and the report close this."""

    def _payload(self, cmd):
        return {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": cmd},
        }

    def _patch_downstream(self, guard_module, monkeypatch):
        # Everything AFTER mergeability passes, so only mergeability decides.
        monkeypatch.setenv(
            "_TEST_GH_CI_ROLLUP",
            json.dumps([{"name": "test", "workflowName": "CI", "conclusion": "SUCCESS"}]),
        )
        for name in ("_check_pr_review_findings", "_check_inline_review_findings"):
            monkeypatch.setattr(guard_module, name, lambda n, force=False, repo=None: (False, ""))
        monkeypatch.setattr(
            guard_module, "_check_base_is_default", lambda n, force=False, repo=None: (False, "")
        )
        monkeypatch.setattr(
            guard_module,
            "_check_codex_reviewed_head",
            lambda n, force=False, repo=None: (False, "", None),
        )
        monkeypatch.setattr(
            guard_module,
            "_check_scheduled_claude_reviewed_head",
            lambda n, head=None, repo=None, force=False, strict=False: None,
        )
        # The pin-receipt gate is downstream too. Without this it runs for real
        # against the cwd repo's PR — whose tree predates scripts/lib/cc_version.sh,
        # so the read is a genuine 404 and the gate correctly BLOCKS, which has
        # nothing to do with the invariant under test here.
        monkeypatch.setattr(guard_module, "_check_pin_receipts", lambda n, repo=None: (False, ""))

    @pytest.mark.parametrize("bad", [None, "", "UNKNOWN", "SOMETHING_NEW"])
    def test_non_mergeable_blocks(self, guard_module, monkeypatch, bad):
        monkeypatch.setattr(guard_module, "_check_mergeable", lambda n, repo=None: bad)
        self._patch_downstream(guard_module, monkeypatch)
        monkeypatch.setattr(
            guard_module, "read_payload", lambda: self._payload("gh pr merge 5 --squash --admin")
        )
        assert guard_module.main() == 2

    def test_conflicting_blocks_with_specific_message(self, guard_module, monkeypatch, capsys):
        monkeypatch.setattr(guard_module, "_check_mergeable", lambda n, repo=None: "CONFLICTING")
        self._patch_downstream(guard_module, monkeypatch)
        monkeypatch.setattr(
            guard_module, "read_payload", lambda: self._payload("gh pr merge 5 --squash --admin")
        )
        assert guard_module.main() == 2
        assert "merge conflicts" in capsys.readouterr().err

    def test_mergeable_passes(self, guard_module, monkeypatch):
        monkeypatch.setattr(guard_module, "_check_mergeable", lambda n, repo=None: "MERGEABLE")
        self._patch_downstream(guard_module, monkeypatch)
        monkeypatch.setattr(
            guard_module, "read_payload", lambda: self._payload("gh pr merge 5 --squash --admin")
        )
        assert guard_module.main() == 0

    def test_report_counts_none_as_failure(self, guard_module, monkeypatch, capsys):
        monkeypatch.setattr(guard_module, "_check_mergeable", lambda n, repo=None: None)
        monkeypatch.setattr(guard_module, "_pr_ci_status", lambda n, repo=None: ("green", []))
        monkeypatch.setattr(
            guard_module, "_check_base_is_default", lambda n, force=False, repo=None: (False, "")
        )
        monkeypatch.setattr(
            guard_module,
            "_check_codex_reviewed_head",
            lambda n, force=False, repo=None: (False, "", None),
        )
        for name in ("_check_pr_review_findings", "_check_inline_review_findings"):
            monkeypatch.setattr(guard_module, name, lambda n, repo=None, strict=False: (False, ""))
        assert guard_module.check_pr_report("5") == 1
        assert "would block" in capsys.readouterr().out


class TestRepoDerivationGate:
    """F3: a bare (no --repo) merge is gated against the repo gh will ACTUALLY
    target — derived from the merge's effective cwd — not the hook process's cwd.
    Fail-closed when that cwd is ambiguous or gh can't resolve a repo there."""

    def _payload(self, cmd, cwd=None):
        p = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": cmd}}
        if cwd is not None:
            p["cwd"] = cwd
        return p

    def _patch_ok(self, guard_module, monkeypatch):
        monkeypatch.setenv(
            "_TEST_GH_CI_ROLLUP",
            json.dumps([{"name": "test", "workflowName": "CI", "conclusion": "SUCCESS"}]),
        )
        monkeypatch.setattr(guard_module, "_check_mergeable", lambda n, repo=None: "MERGEABLE")
        for name in ("_check_pr_review_findings", "_check_inline_review_findings"):
            monkeypatch.setattr(guard_module, name, lambda n, force=False, repo=None: (False, ""))
        monkeypatch.setattr(
            guard_module, "_check_base_is_default", lambda n, force=False, repo=None: (False, "")
        )
        monkeypatch.setattr(
            guard_module,
            "_check_codex_reviewed_head",
            lambda n, force=False, repo=None: (False, "", None),
        )
        monkeypatch.setattr(
            guard_module,
            "_check_scheduled_claude_reviewed_head",
            lambda n, head=None, repo=None, force=False, strict=False: None,
        )
        # The pin-receipt gate is downstream too. Without this it runs for real
        # against the cwd repo's PR — whose tree predates scripts/lib/cc_version.sh,
        # so the read is a genuine 404 and the gate correctly BLOCKS, which has
        # nothing to do with the invariant under test here.
        monkeypatch.setattr(guard_module, "_check_pin_receipts", lambda n, repo=None: (False, ""))

    def test_derived_repo_threads_into_gates(self, guard_module, monkeypatch):
        seen = {}
        monkeypatch.setenv("_TEST_GH_DERIVED_REPO", "octo/other")
        self._patch_ok(guard_module, monkeypatch)

        def rec_mergeable(n, repo=None):
            seen["repo"] = repo
            return "MERGEABLE"

        monkeypatch.setattr(guard_module, "_check_mergeable", rec_mergeable)
        monkeypatch.setattr(
            guard_module,
            "read_payload",
            lambda: self._payload("gh pr merge 5 --squash --admin", cwd="/some/other/checkout"),
        )
        assert guard_module.main() == 0
        assert seen["repo"] == "octo/other"

    def test_explicit_repo_skips_derivation(self, guard_module, monkeypatch):
        seen = {}
        # Seam would derive octo/other, but --repo must win (no derivation).
        monkeypatch.setenv("_TEST_GH_DERIVED_REPO", "octo/other")
        self._patch_ok(guard_module, monkeypatch)

        def rec_mergeable(n, repo=None):
            seen["repo"] = repo
            return "MERGEABLE"

        monkeypatch.setattr(guard_module, "_check_mergeable", rec_mergeable)
        monkeypatch.setattr(
            guard_module,
            "read_payload",
            lambda: self._payload(
                "gh pr merge 5 --repo real/repo --squash --admin", cwd="/some/other/checkout"
            ),
        )
        assert guard_module.main() == 0
        assert seen["repo"] == "real/repo"

    def test_unresolvable_cwd_repo_blocks(self, guard_module, monkeypatch):
        # cwd resolves but gh can't name a repo there (seam empty) → fail-closed.
        monkeypatch.setenv("_TEST_GH_DERIVED_REPO", "")
        self._patch_ok(guard_module, monkeypatch)
        monkeypatch.setattr(
            guard_module,
            "read_payload",
            lambda: self._payload("gh pr merge 5 --squash --admin", cwd="/not/a/repo"),
        )
        assert guard_module.main() == 2

    def test_ambiguous_cd_blocks(self, guard_module, monkeypatch):
        # A `cd` to an unresolvable target (shell var) → _CWD_UNKNOWN → fail-closed,
        # even though a derivation seam is set.
        monkeypatch.setenv("_TEST_GH_DERIVED_REPO", "octo/other")
        self._patch_ok(guard_module, monkeypatch)
        monkeypatch.setattr(
            guard_module,
            "read_payload",
            lambda: self._payload("cd $HOME && gh pr merge 5 --squash --admin", cwd="/repo"),
        )
        assert guard_module.main() == 2

    def test_numberless_merge_threads_derived_cwd_to_resolver(self, guard_module, monkeypatch):
        # Regression (architect audit, round 5): F3 derives an explicit repo for a
        # numberless merge; _resolve_pr_number must get the DERIVED cwd so the
        # branch PR still resolves (an explicit --repo would error). Without the
        # cwd thread, a numberless `gh pr merge` that worked pre-F3 would block.
        seen = {}
        monkeypatch.setenv("_TEST_GH_DERIVED_REPO", "octo/other")
        self._patch_ok(guard_module, monkeypatch)

        def rec_resolve(cmd, repo=None, cwd=None):
            seen["repo"] = repo
            seen["cwd"] = cwd
            return "5"

        monkeypatch.setattr(guard_module, "_resolve_pr_number", rec_resolve)
        monkeypatch.setattr(
            guard_module,
            "read_payload",
            lambda: self._payload("gh pr merge --squash --admin", cwd="/some/other/checkout"),
        )
        assert guard_module.main() == 0
        assert seen["repo"] == "octo/other"
        assert seen["cwd"] == "/some/other/checkout"

    def test_no_cwd_info_keeps_cwd_behavior(self, guard_module, monkeypatch):
        # Payload without cwd (older CC / test harness): nothing to derive from →
        # merge_repo stays None (today's cwd-based behavior), NOT a block.
        seen = {}
        monkeypatch.setenv("_TEST_GH_DERIVED_REPO", "octo/other")
        self._patch_ok(guard_module, monkeypatch)

        def rec_mergeable(n, repo=None):
            seen["repo"] = repo
            return "MERGEABLE"

        monkeypatch.setattr(guard_module, "_check_mergeable", rec_mergeable)
        monkeypatch.setattr(
            guard_module, "read_payload", lambda: self._payload("gh pr merge 5 --squash --admin")
        )
        assert guard_module.main() == 0
        assert seen["repo"] is None  # not derived → cwd repo (unchanged behavior)


class TestUnreadableScanFailsClosed:
    """PR #1434: a finding scan that could not be READ (gh error/timeout/malformed)
    or completed (clipped budget) ALWAYS fails CLOSED — it blocks a merge and shows as
    a failure line in the report. The old fail-OPEN merge path (a silent pass) was the
    CRITICAL. An EMPTY result read COMPLETELY (Codex quota / no comments) stays clean."""

    def test_body_error_fails_closed(self, guard_module):
        with patch.object(
            guard_module.subprocess,
            "run",
            return_value=subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="x"),
        ):
            block, msg = guard_module._check_pr_review_findings("1")
        assert block is True and "unreadable" in msg.lower()

    def test_body_empty_is_clean(self, guard_module):
        # Empty (quota / no comments) is NOT an error — a complete empty read is clean.
        # Under `--paginate --jq '.[] | {…}'` gh emits nothing (not "[]") for
        # zero comments, so the empty output is an empty string.
        with patch.object(
            guard_module.subprocess,
            "run",
            return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout=""),
        ):
            block, _ = guard_module._check_pr_review_findings("1")
        assert block is False

    def test_inline_error_fails_closed(self, guard_module):
        with patch.object(
            guard_module.subprocess,
            "run",
            return_value=subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="x"),
        ):
            block, msg = guard_module._check_inline_review_findings("1")
        assert block is True and "unreadable" in msg.lower()

    def test_report_counts_unreadable_scan_as_failure(self, guard_module, monkeypatch, capsys):
        # The canonical report must not print "all gates pass" when a scan errored.
        monkeypatch.setattr(guard_module, "_check_mergeable", lambda n, repo=None: "MERGEABLE")
        monkeypatch.setattr(guard_module, "_pr_ci_status", lambda n, repo=None: ("green", []))
        monkeypatch.setattr(
            guard_module, "_check_base_is_default", lambda n, force=False, repo=None: (False, "")
        )
        monkeypatch.setattr(
            guard_module,
            "_check_codex_reviewed_head",
            lambda n, force=False, repo=None: (False, "", "h"),
        )
        # Body scan unreadable → block (fail-closed); inline clean.
        monkeypatch.setattr(
            guard_module,
            "_check_pr_review_findings",
            lambda n, repo=None: (True, "could not read review-body comments"),
        )
        monkeypatch.setattr(
            guard_module,
            "_check_inline_review_findings",
            lambda n, repo=None: (False, ""),
        )
        assert guard_module.check_pr_report("5") == 1
        out = capsys.readouterr().out
        assert "would block" in out and "all gates pass" not in out


class TestGateOrdering:
    """F1: Codex freshness must be established BEFORE the finding scans.

    If a review is published between the finding scans and the freshness check,
    its matching commit_id passes freshness while any P1 comments published with
    it were never scanned — the merge proceeds with unread findings. Ordering:
    mergeable → CI → base-invariant → freshness → body findings → inline findings.
    """

    def _payload(self, cmd):
        return {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": cmd},
        }

    def test_freshness_precedes_finding_scans(self, guard_module, monkeypatch):
        calls: list[str] = []
        monkeypatch.setattr(guard_module, "_check_mergeable", lambda n, repo=None: "MERGEABLE")
        monkeypatch.setenv(
            "_TEST_GH_CI_ROLLUP",
            json.dumps([{"name": "test", "workflowName": "CI", "conclusion": "SUCCESS"}]),
        )
        monkeypatch.setattr(
            guard_module,
            "_check_base_is_default",
            lambda n, force=False, repo=None: (calls.append("base"), (False, ""))[1],
        )
        monkeypatch.setattr(
            guard_module,
            "_check_codex_reviewed_head",
            lambda n, force=False, repo=None: (calls.append("freshness"), (False, "", None))[1],
        )
        monkeypatch.setattr(
            guard_module,
            "_check_scheduled_claude_reviewed_head",
            lambda n, head=None, repo=None, force=False, strict=False: (
                calls.append("scheduled"),
                None,
            )[1],
        )
        # Downstream of the ordering under test, and it would otherwise run live
        # against a PR whose tree predates scripts/lib/cc_version.sh (a real 404).
        monkeypatch.setattr(guard_module, "_check_pin_receipts", lambda n, repo=None: (False, ""))
        monkeypatch.setattr(
            guard_module,
            "_check_pr_review_findings",
            lambda n, force=False, repo=None: (calls.append("body"), (False, ""))[1],
        )
        monkeypatch.setattr(
            guard_module,
            "_check_inline_review_findings",
            lambda n, force=False, repo=None: (calls.append("inline"), (False, ""))[1],
        )
        monkeypatch.setattr(
            guard_module,
            "read_payload",
            lambda: self._payload("gh pr merge 5 --squash --admin"),
        )
        assert guard_module.main() == 0
        assert "freshness" in calls and "body" in calls and "inline" in calls
        assert calls.index("freshness") < calls.index("body")
        assert calls.index("freshness") < calls.index("inline")

    def test_report_freshness_precedes_finding_scans(self, guard_module, monkeypatch, capsys):
        calls: list[str] = []
        monkeypatch.setattr(guard_module, "_check_mergeable", lambda n, repo=None: "MERGEABLE")
        monkeypatch.setattr(guard_module, "_pr_ci_status", lambda n, repo=None: ("green", []))
        monkeypatch.setattr(
            guard_module,
            "_check_base_is_default",
            lambda n, force=False, repo=None: (calls.append("base"), (False, ""))[1],
        )
        monkeypatch.setattr(
            guard_module,
            "_check_codex_reviewed_head",
            lambda n, force=False, repo=None: (calls.append("freshness"), (False, "", "h"))[1],
        )
        monkeypatch.setattr(
            guard_module,
            "_check_pr_review_findings",
            lambda n, repo=None: (calls.append("body"), (False, ""))[1],
        )
        monkeypatch.setattr(
            guard_module,
            "_check_inline_review_findings",
            lambda n, repo=None: (calls.append("inline"), (False, ""))[1],
        )
        guard_module.check_pr_report("5")
        assert calls.index("freshness") < calls.index("body")
        assert calls.index("freshness") < calls.index("inline")


class TestLowBFreshnessBackstop:
    """PR #1434 LOW-b (verify-only): the review-body reverse-walk can fall through a
    non-verdict newest comment to an OLDER stale CLEAN verdict and report clean. That is
    NOT sufficient to authorize a merge on its own — the freshness gate
    (_check_codex_reviewed_head) independently requires a CURRENT review at HEAD and runs
    BEFORE the finding scans. So a stale-clean body scan cannot smuggle a merge past a
    head with no current review: freshness blocks first. This test pins that backstop."""

    def _payload(self, cmd):
        return {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": cmd},
        }

    def test_stale_clean_body_scan_blocked_by_freshness(self, guard_module, monkeypatch):
        reached = {"body": False}
        monkeypatch.setattr(guard_module, "_check_mergeable", lambda n, repo=None: "MERGEABLE")
        monkeypatch.setenv(
            "_TEST_GH_CI_ROLLUP",
            json.dumps([{"name": "test", "workflowName": "CI", "conclusion": "SUCCESS"}]),
        )
        monkeypatch.setattr(
            guard_module, "_check_base_is_default", lambda n, force=False, repo=None: (False, "")
        )
        # Freshness FAILS: no current Codex review at head (the realistic stale-head case).
        monkeypatch.setattr(
            guard_module,
            "_check_codex_reviewed_head",
            lambda n, force=False, repo=None: (True, "no current review at head", None),
        )
        # Body scan WOULD return stale-clean — but must never be reached / never decisive.
        def _body(n, force=False, repo=None):
            reached["body"] = True
            return (False, "")
        monkeypatch.setattr(guard_module, "_check_pr_review_findings", _body)
        monkeypatch.setattr(
            guard_module, "read_payload", lambda: self._payload("gh pr merge 5 --squash --admin")
        )
        # Merge is BLOCKED by freshness, despite the body scan being clean.
        assert guard_module.main() == 2
        assert reached["body"] is False  # freshness gated first — stale-clean never decided it


class TestMultiMergeBlocked:
    """P2 fix: multiple merge segments (or merge+push) in one command are blocked
    — the CI/review gates only inspect the first, so a second could smuggle past."""

    def test_two_merges_blocked(self):
        res = _run_guard("gh pr merge 1 --squash --admin && gh pr merge 2 --squash --admin")
        assert res.returncode == 2
        assert "multiple publish/merge" in res.stderr

    def test_merge_plus_push_blocked(self):
        res = _run_guard("gh pr merge 1 --squash --admin && git push origin x")
        assert res.returncode == 2

    def test_single_merge_not_tripped_by_multi_guard(self):
        res = _run_guard("gh pr merge 1 --squash --admin")
        assert "multiple publish/merge" not in res.stderr


_NEL = "\u0085"  # Unicode NEL: splitlines() breaks on it, split("\n") does not


def _cp(rows, rc=0):
    """A CompletedProcess whose stdout is JSONL of the given already-serialized rows."""
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout="\n".join(rows), stderr="")


class TestFetchCommentsPaged:
    """The shared paginated comment-fetch primitive (_fetch_comments_paged):
    accumulates pages, distinguishes first-page vs later-page failure, is NEL-safe,
    and drops non-dict lines (Codex A: P1b partial-read + P2 NEL/non-dict)."""

    def test_accumulates_across_pages_until_short_page(self, guard_module):
        p1 = [json.dumps({"login": "a", "body": f"c{i}"}) for i in range(100)]  # full → fetch p2
        p2 = [json.dumps({"login": "a", "body": f"d{i}"}) for i in range(50)]   # short → last
        with patch.object(guard_module.subprocess, "run", side_effect=[_cp(p1), _cp(p2)]):
            objs, complete = guard_module._fetch_comments_paged("issues/1/comments", "1", None, ".[]")
        assert len(objs) == 150
        assert complete is True

    def test_first_page_error_returns_none_incomplete(self, guard_module):
        with patch.object(guard_module.subprocess, "run", return_value=_cp([], rc=1)):
            objs, complete = guard_module._fetch_comments_paged("issues/1/comments", "1", None, ".[]")
        assert objs is None and complete is False

    def test_first_page_exception_returns_none_incomplete(self, guard_module):
        with patch.object(guard_module.subprocess, "run",
                          side_effect=subprocess.TimeoutExpired(cmd="gh", timeout=8)):
            objs, complete = guard_module._fetch_comments_paged("issues/1/comments", "1", None, ".[]")
        assert objs is None and complete is False

    def test_later_page_failure_keeps_accumulated_but_incomplete(self, guard_module):
        p1 = [json.dumps({"login": "a", "body": f"c{i}"}) for i in range(100)]
        with patch.object(guard_module.subprocess, "run",
                          side_effect=[_cp(p1), subprocess.TimeoutExpired(cmd="gh", timeout=8)]):
            objs, complete = guard_module._fetch_comments_paged("issues/1/comments", "1", None, ".[]")
        assert len(objs) == 100 and complete is False

    def test_nel_inside_body_does_not_fragment(self, guard_module):
        # A raw U+0085 (NEL) inside a JSON string must NOT split the JSONL line
        # (splitlines would → both fragments fail json.loads → the comment is lost).
        line = json.dumps({"login": "a", "body": "head" + _NEL + "tail"}, ensure_ascii=False)
        assert "" + _NEL + "" in line  # the raw NEL is present in the emitted line
        with patch.object(guard_module.subprocess, "run", return_value=_cp([line])):
            objs, complete = guard_module._fetch_comments_paged("issues/1/comments", "1", None, ".[]")
        assert len(objs) == 1 and objs[0]["body"] == "head" + _NEL + "tail" and complete is True

    def test_non_dict_lines_dropped(self, guard_module):
        rows = [json.dumps({"login": "a", "body": "ok"}), json.dumps("bare string"), json.dumps(42)]
        with patch.object(guard_module.subprocess, "run", return_value=_cp(rows)):
            objs, complete = guard_module._fetch_comments_paged("issues/1/comments", "1", None, ".[]")
        assert objs == [{"login": "a", "body": "ok"}] and complete is True

    def test_out_of_budget_before_page1_blocks_without_calling_gh(self, guard_module, monkeypatch):
        # HIGH (PR #1434): an already-expired merge deadline stops the loop BEFORE any gh
        # call — page 1 → (None, incomplete) → caller fails closed. No unbounded gh spawns.
        monkeypatch.setattr(guard_module, "_merge_deadline", guard_module.time.monotonic() - 1.0)
        with patch.object(guard_module.subprocess, "run") as run_mock:
            objs, complete = guard_module._fetch_comments_paged("issues/1/comments", "1", None, ".[]")
        assert objs is None and complete is False
        run_mock.assert_not_called()

    def test_budget_exhausted_mid_flood_stops_early(self, guard_module, monkeypatch):
        # A comment-flood where every page SUCCEEDS fast: the between-page deadline check
        # must stop the loop once the budget is spent, returning INCOMPLETE (fail-closed),
        # instead of spawning all 100 pages and blowing the hook's wall-clock.
        full = [json.dumps({"login": "a", "body": f"c{i}"}) for i in range(100)]  # full → wants next
        clock = {"t": 0.0}
        monkeypatch.setattr(guard_module.time, "monotonic", lambda: clock["t"])
        monkeypatch.setattr(guard_module, "_merge_deadline", 5.0)
        def run_and_advance(*a, **k):
            clock["t"] += 3.0  # each page burns 3s; 5s deadline crossed after 2 pages
            return _cp(full)
        with patch.object(guard_module.subprocess, "run", side_effect=run_and_advance) as run_mock:
            objs, complete = guard_module._fetch_comments_paged("issues/1/comments", "1", None, ".[]")
        assert complete is False  # stopped early → incomplete → caller blocks
        assert run_mock.call_count < guard_module._MAX_COMMENT_PAGES  # NOT all 100 pages
        assert len(objs) == 200  # two full pages read before the budget gate fired

    def test_all_unparseable_page_is_unreadable(self, guard_module):
        # LOW (PR #1434 defense-in-depth): a non-empty page whose every line fails
        # json.loads (malformed body, rc==0) is unreadable, NOT a clean short page.
        garbage = _cp(["this is not json", "{broken", "<html>error</html>"])
        with patch.object(guard_module.subprocess, "run", return_value=garbage):
            objs, complete = guard_module._fetch_comments_paged("issues/1/comments", "1", None, ".[]")
        assert objs is None and complete is False


_BODY_ERR = "### ERROR — raw SQL in production code"


def _body_out(triples):
    """gh output for the review-body scanner: (login, type, body) triples, oldest→newest."""
    return _cp([json.dumps({"login": lg, "type": tp, "body": bd}) for lg, tp, bd in triples])


def _codex_page_newest(newest_login, newest_body):
    """A FULL page (100 rows: 99 codex notes + 1 newest) so the helper fetches page 2."""
    rows = [json.dumps({"login": "chatgpt-codex-connector[bot]", "type": "Bot", "body": f"note {i}"})
            for i in range(99)]
    rows.append(json.dumps({"login": newest_login, "type": "Bot", "body": newest_body}))
    return _cp(rows)


class TestReviewBodyVerdictBearing:
    """Review-body scanner: only a recognized review bot's verdict marker sets state
    (Codex A P1a — a non-verdict status/dependabot comment no longer masks a finding),
    NEL bodies parse (P2), and a seen finding survives an incomplete read (P1b)."""

    def test_github_actions_status_after_error_still_blocks(self, guard_module):
        # github-actions[bot] is IN _REVIEW_BOTS but its CI comment carries no verdict
        # marker; newer than the codex ERROR, it must NOT clear the finding.
        with patch.object(guard_module.subprocess, "run", return_value=_body_out([
            ("chatgpt-codex-connector[bot]", "Bot", _BODY_ERR),
            ("github-actions[bot]", "Bot", "CI run succeeded — all checks green."),
        ])):
            block, _ = guard_module._check_pr_review_findings("100")
        assert block

    def test_dependabot_comment_after_error_still_blocks(self, guard_module):
        with patch.object(guard_module.subprocess, "run", return_value=_body_out([
            ("chatgpt-codex-connector[bot]", "Bot", _BODY_ERR),
            ("dependabot[bot]", "Bot", "Bumped urllib3 from 2.0 to 2.1."),
        ])):
            block, _ = guard_module._check_pr_review_findings("100")
        assert block

    def test_nel_in_error_body_parses_and_blocks(self, guard_module):
        line = json.dumps(
            {"login": "chatgpt-codex-connector[bot]", "type": "Bot",
             "body": _BODY_ERR + "" + _NEL + " (see thread)"},
            ensure_ascii=False,
        )
        assert "" + _NEL + "" in line
        with patch.object(guard_module.subprocess, "run",
                          return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout=line, stderr="")):
            block, _ = guard_module._check_pr_review_findings("100")
        assert block

    def test_finding_on_page1_survives_later_page_timeout(self, guard_module):
        with patch.object(guard_module.subprocess, "run",
                          side_effect=[_codex_page_newest("chatgpt-codex-connector[bot]", _BODY_ERR),
                                       subprocess.TimeoutExpired(cmd="gh", timeout=8)]):
            block, _ = guard_module._check_pr_review_findings("100")
        assert block  # the seen ERROR stands despite the later-page timeout

    def test_clean_page1_incomplete_read_fails_closed(self, guard_module):
        # Newest verdict is clean, but a later page failed → cannot confirm no newer
        # finding. Fail-closed (PR #1434): the incomplete read BLOCKS, not a silent pass.
        def se():
            return [_codex_page_newest("chatgpt-codex-connector[bot]", "VERDICT: PASS"),
                    subprocess.TimeoutExpired(cmd="gh", timeout=8)]
        with patch.object(guard_module.subprocess, "run", side_effect=se()):
            block, msg = guard_module._check_pr_review_findings("100")
        assert block is True
        assert "unreadable" in msg.lower()


class TestInlinePaginationRobustness:
    """Inline scanner shares the paged fetch: NEL bodies parse, and a P1 on an early
    page survives a later-page failure; no-P1 + incomplete read fails closed in strict."""

    def _codex(self, cid, body, reply_to=None):
        return {"id": cid, "reply_to": reply_to, "login": "chatgpt-codex-connector[bot]",
                "type": "Bot", "body": body}

    def test_nel_in_inline_p1_parses_and_blocks(self, guard_module):
        c = self._codex(1, _P1_BODY.replace("Details here.", "Details" + _NEL + "here."))
        line = json.dumps(c, ensure_ascii=False)
        assert "" + _NEL + "" in line
        with patch.object(guard_module.subprocess, "run",
                          return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout=line, stderr="")):
            block, _ = guard_module._check_inline_review_findings("100")
        assert block

    def test_p1_on_page1_survives_later_page_timeout(self, guard_module):
        rows = [json.dumps(self._codex(i, "note")) for i in range(99)]
        rows.append(json.dumps(self._codex(99, _P1_BODY)))  # newest, full page → fetch p2
        with patch.object(guard_module.subprocess, "run",
                          side_effect=[_cp(rows), subprocess.TimeoutExpired(cmd="gh", timeout=8)]):
            block, _ = guard_module._check_inline_review_findings("100")
        assert block

    def test_no_p1_incomplete_read_fails_closed(self, guard_module):
        rows = [json.dumps(self._codex(i, "just a note")) for i in range(100)]  # full page, no P1
        def se():
            return [_cp(rows), subprocess.TimeoutExpired(cmd="gh", timeout=8)]
        # No P1 seen, but a later page failed → cannot confirm absence. Fail-closed
        # (PR #1434): the incomplete read BLOCKS.
        with patch.object(guard_module.subprocess, "run", side_effect=se()):
            block, msg = guard_module._check_inline_review_findings("100")
        assert block is True
        assert "unreadable" in msg.lower()
