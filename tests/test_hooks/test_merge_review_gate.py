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

# Resolve the hook script path
_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "hooks"
_GUARD = _SCRIPTS / "git_push_guard.py"

sys.path.insert(0, str(_SCRIPTS))
from shell_parse import (  # noqa: E402
    analyze,
    gh_pr_subcommand,
    has_trailing_override,
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


def _rc(code: int) -> subprocess.CompletedProcess:
    """A fake CompletedProcess carrying just a returncode."""
    return subprocess.CompletedProcess(args=[], returncode=code)


def _proc(code: int, out: str = "") -> subprocess.CompletedProcess:
    """A fake CompletedProcess carrying a returncode and stdout."""
    return subprocess.CompletedProcess(args=[], returncode=code, stdout=out)


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


# ── _check_pr_review_findings tests ─────────────────────────────────


class TestCheckPrReviewFindings:
    """Unit tests for _check_pr_review_findings()."""

    def _make_gh_output(self, comments: list[tuple[str, str, str]]) -> str:
        """Build mock gh api JSON output: [{login, type, body}, ...]."""
        return json.dumps(
            [{"login": login, "type": utype, "body": body} for login, utype, body in comments]
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

    def test_api_error_fails_open(self, guard_module):
        """gh api returning error → fail-open (allow merge)."""
        with patch.object(guard_module.subprocess, "run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=1,
                stdout="",
                stderr="API error",
            )
            should_block, msg = guard_module._check_pr_review_findings("100")
        assert not should_block

    def test_api_timeout_fails_open(self, guard_module):
        """gh api timeout → fail-open."""
        with patch.object(guard_module.subprocess, "run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd="gh", timeout=15)
            should_block, msg = guard_module._check_pr_review_findings("100")
        assert not should_block

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
        output = json.dumps(
            [
                {"login": "chatgpt-codex-connector[bot]", "type": "Bot", "body": None},
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

    def _codex(self, cid, body, reply_to=None):
        return {
            "id": cid,
            "reply_to": reply_to,
            "login": "chatgpt-codex-connector[bot]",
            "type": "Bot",
            "body": body,
        }

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

    def test_replied_p1_is_acknowledged(self, guard_module):
        comments = [
            self._codex(1, _P1_BODY),
            {
                "id": 2,
                "reply_to": 1,
                "login": "WingedGuardian",
                "type": "User",
                "body": "Fixed in abc123.",
            },
        ]
        with self._mock(guard_module, comments):
            block, _ = guard_module._check_inline_review_findings("100")
        assert not block

    def test_force_override_allows(self, guard_module):
        block, _ = guard_module._check_inline_review_findings(
            "100",
            force=True,
        )
        assert not block

    def test_api_error_fails_open(self, guard_module):
        with self._mock(guard_module, [], rc=1):
            block, _ = guard_module._check_inline_review_findings("100")
        assert not block

    def test_gh_call_paginates(self, guard_module):
        # Findings beyond REST page 1 (30 comments) must still gate —
        # the very first PR through this gate (#996) drew a P1 for it.
        with self._mock(guard_module, []) as run_mock:
            guard_module._check_inline_review_findings("100")
        assert "--paginate" in run_mock.call_args[0][0]

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
        ],
    )
    def test_stops_at_shell_separator(self, guard_module, cmd, expected):
        assert guard_module._extract_pr_number(cmd) == expected

    def test_quoted_digits_not_a_pr_number(self, guard_module):
        # shlex keeps the quoted arg whole — '123' inside --subject must
        # not resolve as the PR number.
        cmd = 'gh pr merge --subject "fix 123"'
        assert guard_module._extract_pr_number(cmd) is None

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
                {"name": "test", "status": "COMPLETED", "conclusion": "SUCCESS"},
                {"name": "lint", "status": "COMPLETED", "conclusion": "SUCCESS"},
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
                {"name": "test", "conclusion": "SUCCESS"},
                {"name": "opt", "conclusion": "SKIPPED"},
                {"name": "info", "conclusion": "NEUTRAL"},
            ],
        )
        assert guard_module._pr_ci_status("1") == ("green", [])

    def test_legacy_statuscontext_failure_is_red(self, guard_module, monkeypatch):
        self._set(monkeypatch, [{"context": "legacy-ci", "state": "FAILURE"}])
        assert guard_module._pr_ci_status("1") == ("red", ["legacy-ci"])

    def test_empty_is_unknown(self, guard_module, monkeypatch):
        monkeypatch.setenv("_TEST_GH_CI_ROLLUP", "[]")
        assert guard_module._pr_ci_status("1") == ("unknown", [])

    def test_garbage_is_unknown(self, guard_module, monkeypatch):
        monkeypatch.setenv("_TEST_GH_CI_ROLLUP", "not-json")
        assert guard_module._pr_ci_status("1") == ("unknown", [])

    def test_non_ci_payload_is_unknown_fail_open(self, guard_module, monkeypatch):
        # e.g. a review-comment mock other tests inject — must not be read as red.
        self._set(monkeypatch, [{"login": "codex", "body": "FAILURE somewhere"}])
        assert guard_module._pr_ci_status("1") == ("unknown", [])


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
        monkeypatch.setattr(guard_module, "_check_mergeable", lambda n: "MERGEABLE")
        monkeypatch.setattr(
            guard_module, "_check_pr_review_findings", lambda n, force=False: (False, "")
        )
        monkeypatch.setattr(
            guard_module,
            "_check_inline_review_findings",
            lambda n, force=False: (False, ""),
        )

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
            "_TEST_GH_CI_ROLLUP", json.dumps([{"name": "test", "conclusion": "SUCCESS"}])
        )
        self._patch_merge_ok(guard_module, monkeypatch)
        monkeypatch.setattr(
            guard_module,
            "read_payload",
            lambda: self._payload("gh pr merge 5 --squash --admin"),
        )
        assert guard_module.main() == 0


class TestCiStatusUnfinishedStates:
    """P1 fix: ANY non-terminal check status counts as pending (not just
    QUEUED/IN_PROGRESS) — a new/renamed unfinished state can't read as green."""

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
            {"name": "lint", "status": "COMPLETED", "conclusion": "SUCCESS"},
        ])
        assert guard_module._pr_ci_status("1") == ("green", [])


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
