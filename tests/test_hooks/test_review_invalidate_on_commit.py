"""The marker must survive a commit that did not happen.

`review_invalidate_on_commit` clears the review marker after a commit so the
NEXT one needs a fresh review. It previously read stdout and stderr and then
ignored both (two `noqa: F841` locals), leaving an `error` key as the only
signal — which neither real failure mode sets:

  1. a PreToolUse BLOCK (the review gate itself, or another guard): the tool
     never ran at all;
  2. a git-level failure (`fatal: could not read log file ...`).

Both were MEASURED clearing the marker for a commit that produced nothing, which
livelocks the author: mark -> commit refused -> marker gone -> mark -> refused,
with nothing explaining why a freshly-written marker had gone stale.

The fix is deliberately ASYMMETRIC. Over-clearing costs only a re-review;
under-clearing would let a later commit ride a marker that never covered it. So
the unknown case still invalidates, exactly as before — only the readable-stdout
path gained a positive-evidence check.
"""

from __future__ import annotations

import contextlib
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "scripts"))
sys.path.insert(0, str(_ROOT / "scripts" / "hooks"))

import review_invalidate_on_commit as inv  # noqa: E402


class TestCommitSuccessDetection:
    def test_matches_an_ordinary_commit_line(self):
        assert inv._COMMIT_OK.search("[main 1a2b3c4] fix: a thing")

    def test_matches_a_detached_head(self):
        assert inv._COMMIT_OK.search("[detached HEAD 1a2b3c4] fix: a thing")

    def test_matches_a_root_commit(self):
        assert inv._COMMIT_OK.search("[main (root-commit) 1a2b3c4] initial")

    def test_matches_when_preceded_by_other_output(self):
        out = "Running hooks...\n[feat/x 9f8e7d6c] feat: something\n 2 files changed"
        assert inv._COMMIT_OK.search(out)

    def test_does_not_match_a_git_level_failure(self):
        """The exact text of the failure observed in the field."""
        out = "fatal: could not read log file '/tmp/msg.txt': No such file or directory"
        assert not inv._COMMIT_OK.search(out)

    def test_does_not_match_a_guard_block(self):
        out = "BLOCKED: Code changes exist without review. Run an adversarial audit first"
        assert not inv._COMMIT_OK.search(out)

    def test_does_not_match_nothing_to_commit(self):
        assert not inv._COMMIT_OK.search("nothing to commit, working tree clean")

    def test_does_not_match_a_bare_bracket(self):
        """A sha is required — an arbitrary bracketed line is not a commit."""
        assert not inv._COMMIT_OK.search("[INFO] some log line")


class TestMainHonoursTheSuccessCheck:
    """The regex is worthless unless main() consults it.

    Without these, reverting the fix to the old `error`-key-only check leaves
    every test above green — the exact shape of a test that passes for the wrong
    reason.
    """

    def _drive(self, monkeypatch, *, stdout, error=None):
        import io
        import json

        cleared: list = []
        payload = {
            "tool_name": "Bash",
            "tool_input": {"command": "git commit -m x"},
            "cwd": "/tmp",
            "tool_response": {"stdout": stdout, "stderr": ""},
        }
        if error is not None:
            payload["tool_response"]["error"] = error
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
        monkeypatch.setattr(inv, "clear_marker", lambda **kw: cleared.append(kw))
        monkeypatch.setattr(inv, "_over_clear", lambda *a, **k: cleared.append("over"))
        with contextlib.suppress(SystemExit):
            inv.main()
        return cleared

    def test_a_successful_commit_clears_the_marker(self, monkeypatch):
        assert self._drive(monkeypatch, stdout="[main 1a2b3c4] fix: a thing")

    def test_a_git_level_failure_preserves_the_marker(self, monkeypatch):
        out = "fatal: could not read log file '/tmp/msg.txt': No such file or directory"
        assert self._drive(monkeypatch, stdout=out) == [], (
            "a commit that never produced anything must not invalidate the review"
        )

    def test_a_guard_block_preserves_the_marker(self, monkeypatch):
        out = "BLOCKED: Code changes exist without review. Run an adversarial audit first"
        assert self._drive(monkeypatch, stdout=out) == []

    def test_an_explicit_error_still_preserves_the_marker(self, monkeypatch):
        assert self._drive(monkeypatch, stdout="", error="boom") == []

    def test_unreadable_output_still_over_clears(self, monkeypatch):
        """The conservative default is UNCHANGED: unknown -> invalidate.

        Under-clearing would let a later commit ride a marker that never covered
        it, so the fix deliberately did not flip this direction.
        """
        assert self._drive(monkeypatch, stdout="") != []
