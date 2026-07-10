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


def _run_guard(command: str, *, mock_gh_output: str = "", mock_gh_rc: int = 0) -> subprocess.CompletedProcess:
    """Run git_push_guard.py with a mock gh api response.

    We patch subprocess.run inside the hook to intercept gh api calls
    while still letting other subprocess calls (like git branch) work.
    """
    env = {
        **os.environ,
        "CLAUDE_TOOL_INPUT": json.dumps({"command": command}),
        # Inject mock response via env var — the test wrapper reads it
        "_TEST_GH_API_OUTPUT": mock_gh_output,
        "_TEST_GH_API_RC": str(mock_gh_rc),
    }

    # We can't easily mock subprocess inside a subprocess, so we test
    # the _check_pr_review_findings function directly via import instead.
    result = subprocess.run(
        [sys.executable, str(_GUARD)],
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


# ── _check_pr_review_findings tests ─────────────────────────────────

class TestCheckPrReviewFindings:
    """Unit tests for _check_pr_review_findings()."""

    def _make_gh_output(self, comments: list[tuple[str, str, str]]) -> str:
        """Build mock gh api JSON output: [{login, type, body}, ...]."""
        return json.dumps([
            {"login": login, "type": utype, "body": body}
            for login, utype, body in comments
        ])

    def test_no_comments_allows_merge(self, guard_module):
        """No review comments at all → fail-open (quota exhausted case)."""
        with patch.object(guard_module.subprocess, "run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr="",
            )
            should_block, msg = guard_module._check_pr_review_findings("100")
        assert not should_block
        assert msg == ""

    def test_clean_review_allows_merge(self, guard_module):
        """Review comment with CLEAN verdict → allow."""
        output = self._make_gh_output([
            ("chatgpt-codex-connector[bot]", "Bot",
             "## Structural Review\n\nNo issues.\n\n## PII / Secrets / Wording scan: **CLEAN**"),
        ])
        with patch.object(guard_module.subprocess, "run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=output, stderr="",
            )
            should_block, msg = guard_module._check_pr_review_findings("100")
        assert not should_block

    def test_error_finding_blocks_merge(self, guard_module):
        """Review with ### ERROR blocks merge."""
        output = self._make_gh_output([
            ("chatgpt-codex-connector[bot]", "Bot",
             "## Structural Review\n\n### ERROR — Raw SQL in production code\n\n"
             "`src/genesis/foo.py` uses raw SQL.\n\n"
             "## PII scan: not performed"),
        ])
        with patch.object(guard_module.subprocess, "run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=output, stderr="",
            )
            should_block, msg = guard_module._check_pr_review_findings("100")
        assert should_block
        assert "review-override" in msg

    def test_p1_finding_blocks_merge(self, guard_module):
        """Review with [P1] marker blocks merge."""
        output = self._make_gh_output([
            ("chatgpt-codex-connector[bot]", "Bot",
             "[P1] Logic bug: session_id always None\n"
             "[P2] Missing docstring on helper"),
        ])
        with patch.object(guard_module.subprocess, "run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=output, stderr="",
            )
            should_block, msg = guard_module._check_pr_review_findings("100")
        assert should_block

    def test_hard_block_blocks_merge(self, guard_module):
        """Review with HARD BLOCK blocks merge."""
        output = self._make_gh_output([
            ("chatgpt-codex-connector[bot]", "Bot",
             "### 🔴 HARD BLOCK\n\nPrivate IP found in config file."),
        ])
        with patch.object(guard_module.subprocess, "run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=output, stderr="",
            )
            should_block, msg = guard_module._check_pr_review_findings("100")
        assert should_block

    def test_warning_only_allows_merge(self, guard_module):
        """Review with only WARNINGs (no ERRORs) → allow."""
        output = self._make_gh_output([
            ("chatgpt-codex-connector[bot]", "Bot",
             "## Structural Review\n\n### WARNING — Missing test coverage\n\n"
             "No test for new function.\n\n"
             "## PII / Secrets / Wording scan: **CLEAN**"),
        ])
        with patch.object(guard_module.subprocess, "run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=output, stderr="",
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
                args=[], returncode=1, stdout="", stderr="API error",
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
        output = self._make_gh_output([
            ("chatgpt-codex-connector[bot]", "Bot",
             "## Structural Review\n\n### ERROR — Raw SQL\n\nFix needed."),
            ("chatgpt-codex-connector[bot]", "Bot",
             "## Structural Review\n\nPASS — no issues.\n\n"
             "## PII / Secrets / Wording scan: **CLEAN**\n\nVERDICT: PASS"),
        ])
        with patch.object(guard_module.subprocess, "run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=output, stderr="",
            )
            should_block, msg = guard_module._check_pr_review_findings("100")
        assert not should_block

    def test_human_comments_ignored(self, guard_module):
        """Human comments with 'ERROR' in text are not checked."""
        output = self._make_gh_output([
            ("octocat", "User",
             "### ERROR — I think this is wrong\n\nJust my opinion."),
        ])
        with patch.object(guard_module.subprocess, "run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=output, stderr="",
            )
            should_block, msg = guard_module._check_pr_review_findings("100")
        assert not should_block

    def test_codex_quota_message_not_treated_as_review(self, guard_module):
        """Codex quota-exhausted message without findings → not a review."""
        output = self._make_gh_output([
            ("chatgpt-codex-connector[bot]", "Bot",
             "You have reached your Codex usage limits for code reviews. "
             "You can see your limits in the Codex usage dashboard."),
        ])
        with patch.object(guard_module.subprocess, "run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=output, stderr="",
            )
            should_block, msg = guard_module._check_pr_review_findings("100")
        assert not should_block

    def test_error_in_clean_comment_does_not_block(self, guard_module):
        """Comment mentions ERROR category but scan is CLEAN → no block."""
        output = self._make_gh_output([
            ("chatgpt-codex-connector[bot]", "Bot",
             "**Structural review:** No ERRORs found.\n\n"
             "## PII / Secrets / Wording scan: **CLEAN**"),
        ])
        with patch.object(guard_module.subprocess, "run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=output, stderr="",
            )
            should_block, msg = guard_module._check_pr_review_findings("100")
        assert not should_block

    def test_error_plus_incidental_clean_phrase_blocks(self, guard_module):
        """A real ERROR heading with incidental prose should still block."""
        output = self._make_gh_output([
            ("chatgpt-codex-connector[bot]", "Bot",
             "### ERROR — hardcoded credential\n\nUse env vars.\n\n"
             "No issues found in the formatting section."),
        ])
        with patch.object(guard_module.subprocess, "run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=output, stderr="",
            )
            should_block, msg = guard_module._check_pr_review_findings("100")
        assert should_block

    def test_null_body_fails_open(self, guard_module):
        """GitHub API returning body: null should not crash."""
        output = json.dumps([
            {"login": "chatgpt-codex-connector[bot]", "type": "Bot", "body": None},
        ])
        with patch.object(guard_module.subprocess, "run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=output, stderr="",
            )
            should_block, msg = guard_module._check_pr_review_findings("100")
        assert not should_block

    def test_p2_only_allows_merge(self, guard_module):
        """Review with only [P2] markers → allow."""
        output = self._make_gh_output([
            ("chatgpt-codex-connector[bot]", "Bot",
             "[P2] Missing docstring\n[P2] Inline import"),
        ])
        with patch.object(guard_module.subprocess, "run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=output, stderr="",
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
            guard_module.subprocess, "run",
            return_value=subprocess.CompletedProcess(
                args=[], returncode=rc,
                stdout="\n".join(json.dumps(c) for c in comments),
                stderr="",
            ),
        )

    def _codex(self, cid, body, reply_to=None):
        return {
            "id": cid, "reply_to": reply_to,
            "login": "chatgpt-codex-connector[bot]", "type": "Bot",
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
            {"id": 2, "reply_to": 1, "login": "WingedGuardian",
             "type": "User", "body": "Fixed in abc123."},
        ]
        with self._mock(guard_module, comments):
            block, _ = guard_module._check_inline_review_findings("100")
        assert not block

    def test_force_override_allows(self, guard_module):
        block, _ = guard_module._check_inline_review_findings(
            "100", force=True,
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
        comments = [{
            "id": 1, "reply_to": None, "login": "WingedGuardian",
            "type": "User", "body": _P1_BODY,
        }]
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

    def test_quoted_digits_not_a_pr_number(self, guard_module):
        # shlex keeps the quoted arg whole — '123' inside --subject must
        # not resolve as the PR number.
        cmd = 'gh pr merge --subject "fix 123"'
        assert guard_module._extract_pr_number(cmd) is None

    def test_resolves_current_branch_pr(self, guard_module):
        with patch.object(
            guard_module.subprocess, "run",
            return_value=subprocess.CompletedProcess(
                args=[], returncode=0, stdout="88\n", stderr="",
            ),
        ):
            assert guard_module._resolve_pr_number("gh pr merge --squash") == "88"

    def test_unresolvable_returns_none(self, guard_module):
        with patch.object(
            guard_module.subprocess, "run",
            return_value=subprocess.CompletedProcess(
                args=[], returncode=1, stdout="", stderr="no pr",
            ),
        ):
            assert guard_module._resolve_pr_number("gh pr merge --squash") is None
