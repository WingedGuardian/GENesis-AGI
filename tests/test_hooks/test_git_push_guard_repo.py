"""Tests for git_push_guard cross-repo --repo threading (follow-up 34421c9c).

Incident (2026-07-26): every gh-pr-merge gate ran `gh pr view <N>` /
`gh api repos/:owner/:repo/…` against the CWD repo, so merging a cross-repo PR
checked the WRONG repo's PR #N — a permanent false block when that PR reports
UNKNOWN, or worse a wrong-PR gate pass. The fix parses --repo/-R (or the PR
URL) from the merge segment and threads it through ALL five gates, including
the no-arg `gh pr view` fallback (threading the gates but not the fallback
would resolve the cwd branch's PR NUMBER and gate it against the other repo —
the same bug through the fix).

All tests are network-free: gh invocations are captured via a patched
subprocess.run.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import patch

_WORKTREE = Path(__file__).resolve().parent.parent.parent
_HOOKS_DIR = _WORKTREE / "scripts" / "hooks"

_spec = importlib.util.spec_from_file_location("git_push_guard", _HOOKS_DIR / "git_push_guard.py")
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

_normalize_repo = _mod._normalize_repo
_merge_target_repo = _mod._merge_target_repo


class _FakeResult:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class TestNormalizeRepo:
    def test_owner_repo_passthrough(self):
        assert _normalize_repo("octo/genesis") == "octo/genesis"

    def test_host_owner_repo(self):
        assert _normalize_repo("github.com/octo/genesis") == "octo/genesis"

    def test_https_url(self):
        assert _normalize_repo("https://github.com/octo/genesis") == "octo/genesis"

    def test_shell_variable_unresolvable(self):
        assert _normalize_repo("$REPO") is None

    def test_backtick_unresolvable(self):
        assert _normalize_repo("`cmd`/x") is None

    def test_single_token_invalid(self):
        assert _normalize_repo("justonename") is None

    def test_empty(self):
        assert _normalize_repo("") is None


class TestMergeTargetRepo:
    def test_no_repo_flag(self):
        argv = ["gh", "pr", "merge", "5", "--squash", "--admin"]
        assert _merge_target_repo(argv, "gh pr merge 5 --squash --admin") is None

    def test_long_flag_separate_value(self):
        argv = ["gh", "pr", "merge", "5", "--repo", "octo/voice", "--admin"]
        assert _merge_target_repo(argv, "") == "octo/voice"

    def test_long_flag_equals_form(self):
        argv = ["gh", "pr", "merge", "--repo=octo/voice", "5"]
        assert _merge_target_repo(argv, "") == "octo/voice"

    def test_short_flag_separate_value(self):
        argv = ["gh", "pr", "merge", "-R", "octo/voice", "5"]
        assert _merge_target_repo(argv, "") == "octo/voice"

    def test_short_flag_glued(self):
        argv = ["gh", "pr", "merge", "-Rocto/voice", "5"]
        assert _merge_target_repo(argv, "") == "octo/voice"

    def test_flag_before_subcommand_args(self):
        """pflag accepts the flag anywhere — scan the whole argv."""
        argv = ["gh", "--repo", "octo/voice", "pr", "merge", "5"]
        assert _merge_target_repo(argv, "") == "octo/voice"

    def test_pr_url_in_command(self):
        cmd = "gh pr merge https://github.com/octo/voice/pull/43 --squash --admin"
        argv = ["gh", "pr", "merge", "https://github.com/octo/voice/pull/43"]
        assert _merge_target_repo(argv, cmd) == "octo/voice"

    def test_explicit_flag_wins_over_url(self):
        cmd = "gh pr merge https://github.com/octo/other/pull/9 --repo octo/voice"
        argv = ["gh", "pr", "merge", "https://github.com/octo/other/pull/9", "--repo", "octo/voice"]
        assert _merge_target_repo(argv, cmd) == "octo/voice"

    def test_variable_repo_unresolvable(self):
        argv = ["gh", "pr", "merge", "5", "--repo", "$TARGET"]
        assert _merge_target_repo(argv, "") is None


class TestGateThreading:
    """Each gate's gh invocation must carry the explicit repo."""

    def _capture(self):
        calls: list[list[str]] = []

        def fake_run(args, **kwargs):
            calls.append(list(args))
            return _FakeResult(returncode=1)  # unresolved/failed — content irrelevant

        return calls, fake_run

    def test_check_mergeable_carries_repo(self):
        calls, fake = self._capture()
        with patch.object(_mod.subprocess, "run", fake):
            _mod._check_mergeable("5", repo="octo/voice")
        assert calls and "--repo" in calls[0]
        assert calls[0][calls[0].index("--repo") + 1] == "octo/voice"

    def test_check_mergeable_without_repo_has_no_flag(self):
        calls, fake = self._capture()
        with patch.object(_mod.subprocess, "run", fake):
            _mod._check_mergeable("5")
        assert calls and "--repo" not in calls[0]

    def test_ci_status_carries_repo(self, monkeypatch):
        monkeypatch.delenv("_TEST_GH_CI_ROLLUP", raising=False)
        calls, fake = self._capture()
        with patch.object(_mod.subprocess, "run", fake):
            _mod._pr_ci_status("5", repo="octo/voice")
        assert calls and "--repo" in calls[0]

    def test_review_findings_endpoint_uses_repo(self):
        calls, fake = self._capture()
        with patch.object(_mod.subprocess, "run", fake):
            _mod._check_pr_review_findings("5", repo="octo/voice")
        assert calls
        assert any("repos/octo/voice/issues/5/comments" in tok for tok in calls[0])

    def test_review_findings_endpoint_default_cwd_repo(self):
        calls, fake = self._capture()
        with patch.object(_mod.subprocess, "run", fake):
            _mod._check_pr_review_findings("5")
        assert calls
        assert any("repos/:owner/:repo/issues/5/comments" in tok for tok in calls[0])

    def test_inline_findings_endpoint_uses_repo(self):
        calls, fake = self._capture()
        with patch.object(_mod.subprocess, "run", fake):
            _mod._check_inline_review_findings("5", repo="octo/voice")
        assert calls
        assert any("repos/octo/voice/pulls/5/comments" in tok for tok in calls[0])


class TestResolvePrNumberFallback:
    """The no-arg fallback must honor --repo (red-team finding 5): resolving
    the CWD branch's PR number and gating it against ANOTHER repo would
    re-create the wrong-PR bug through the fix itself."""

    def test_fallback_carries_repo_flag(self):
        calls: list[list[str]] = []

        def fake_run(args, **kwargs):
            calls.append(list(args))
            # gh errors when --repo is given without a PR selector — the
            # coherent outcome is None → the caller fails CLOSED.
            return _FakeResult(returncode=1, stderr="argument required when using --repo")

        with patch.object(_mod.subprocess, "run", fake_run):
            result = _mod._resolve_pr_number("gh pr merge --repo octo/voice", repo="octo/voice")
        assert result is None
        assert calls and "--repo" in calls[0]

    def test_explicit_number_needs_no_gh_call(self):
        calls: list[list[str]] = []

        def fake_run(args, **kwargs):  # pragma: no cover — must not be reached
            calls.append(list(args))
            return _FakeResult()

        with patch.object(_mod.subprocess, "run", fake_run):
            result = _mod._resolve_pr_number("gh pr merge 43 --repo octo/voice", repo="octo/voice")
        assert result == "43"
        assert not calls

    def test_url_number_extraction_still_works(self):
        result = _mod._extract_pr_number("gh pr merge https://github.com/octo/voice/pull/43")
        assert result == "43"
