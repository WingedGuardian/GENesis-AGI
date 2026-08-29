"""The leaks gate honours an EARLIER accepted marker when the mechanical scanner
is green at the CURRENT head.

WHY the relief exists. The scheduled routines are not re-run on a push, so on any
multi-push PR the marker sits at the first head and the gate blocks. Measured over
ten recent PRs: every one with commits after its marker was blocked, and the only
escape was `# scheduled-review-override` -- a sigil that verifies NOTHING. Routine
use of an exception valve is worse than a narrower rule.

WHY IT IS SAFE. The two layers catch different leak classes. The mechanical scanner
catches literal patterns and runs per-head; the scheduled LLM review catches
inferential leaks. Carrying the LLM verdict forward while REQUIRING the mechanical
one at this exact head is strictly more checking than the bare override it replaces.

Every test here pins a cell of the product {marker state} x {scanner state}, because
relief that fires on the wrong cell silently weakens an irreducible gate. The
fail-closed cells matter more than the happy path: a merge gate that mistakes an
unreadable scan for a pass is worse than one that never grants relief at all.

Network-free via the _TEST_GH_* env-injection seams.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_WORKTREE = Path(__file__).resolve().parent.parent.parent
_HOOKS_DIR = _WORKTREE / "scripts" / "hooks"
_spec = importlib.util.spec_from_file_location("git_push_guard", _HOOKS_DIR / "git_push_guard.py")
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

HEAD = "0cd13afeb51025af5dc7bd24df1ffa57cd2babab"
EARLIER = "1111111111111111111111111111111111111111"


def _marker(*kinds, head=EARLIER, body_prefix="scheduled review done. VERDICT: PASS"):
    body = body_prefix + "\n" + "\n".join(
        f"<!-- genesis-scheduled-review: head={head} kind={k} -->" for k in kinds
    )
    return json.dumps({"login": "owner", "author_association": "OWNER", "body": body})


def _check_runs(*runs):
    return json.dumps([{"name": n, "conclusion": c} for n, c in runs])


@pytest.fixture(autouse=True)
def _base(monkeypatch):
    monkeypatch.setenv("_TEST_CANONICAL_PUBLIC_REPO", "acme/pub")
    monkeypatch.setenv("_TEST_REQUIRED_SCHEDULED_REVIEWS", "leaks")


def _gate():
    return _mod._check_scheduled_claude_reviewed_head("1", head_sha=HEAD, repo="acme/pub")


class TestReliefHappyPath:
    def test_earlier_accepted_marker_plus_green_scanner_passes(self, monkeypatch):
        """The whole point: a multi-push PR reviewed once no longer needs an override."""
        monkeypatch.setenv("_TEST_GH_SCHEDULED_COMMENTS", _marker("leaks"))
        monkeypatch.setenv("_TEST_GH_CHECK_RUNS", _check_runs(("leak-detector", "success")))
        assert _gate() is None

    def test_marker_already_at_head_needs_no_relief(self, monkeypatch):
        """Control: the pre-existing pass path is untouched, with NO scanner data at all.

        Without this, a bug that made relief the ONLY way to pass would still look
        green on the happy-path test above.
        """
        monkeypatch.setenv("_TEST_GH_SCHEDULED_COMMENTS", _marker("leaks", head=HEAD))
        monkeypatch.setenv("_TEST_GH_CHECK_RUNS", "[]")
        assert _gate() is None


class TestReliefFailsClosed:
    """Each cell here MUST still block. These are the tests that keep the relief narrow."""

    def test_scanner_failed_blocks(self, monkeypatch):
        monkeypatch.setenv("_TEST_GH_SCHEDULED_COMMENTS", _marker("leaks"))
        monkeypatch.setenv("_TEST_GH_CHECK_RUNS", _check_runs(("leak-detector", "failure")))
        msg = _gate()
        assert msg and "leaks" in msg

    def test_scanner_still_running_blocks(self, monkeypatch):
        """conclusion is null while a run is in flight -- not success, so no relief."""
        monkeypatch.setenv("_TEST_GH_SCHEDULED_COMMENTS", _marker("leaks"))
        monkeypatch.setenv("_TEST_GH_CHECK_RUNS", json.dumps([{"name": "leak-detector"}]))
        msg = _gate()
        assert msg and "leaks" in msg

    def test_scanner_absent_at_head_blocks(self, monkeypatch):
        """A head the scanner never ran on gets no relief -- absence is not success."""
        monkeypatch.setenv("_TEST_GH_SCHEDULED_COMMENTS", _marker("leaks"))
        monkeypatch.setenv("_TEST_GH_CHECK_RUNS", _check_runs(("some-other-job", "success")))
        msg = _gate()
        assert msg and "leaks" in msg

    def test_unreadable_scanner_payload_blocks(self, monkeypatch):
        """An unreadable mechanical scan must never read as a pass."""
        monkeypatch.setenv("_TEST_GH_SCHEDULED_COMMENTS", _marker("leaks"))
        monkeypatch.setenv("_TEST_GH_CHECK_RUNS", "not json at all")
        msg = _gate()
        assert msg and "leaks" in msg

    def test_no_marker_anywhere_blocks_even_when_green(self, monkeypatch):
        """Relief CARRIES a prior review forward; it never manufactures one.

        A green mechanical scan on a PR the LLM reviewer never looked at must still
        block -- otherwise the inferential layer is silently dropped entirely.
        """
        monkeypatch.setenv("_TEST_GH_SCHEDULED_COMMENTS", "")
        monkeypatch.setenv("_TEST_GH_CHECK_RUNS", _check_runs(("leak-detector", "success")))
        msg = _gate()
        assert msg and "leaks" in msg

    def test_refused_earlier_marker_does_not_carry(self, monkeypatch):
        """A routine that ran and FOUND something must not be carried forward.

        The earlier marker is recorded as REFUSED (its body reads as a blocking
        finding), so it lives in the rejected map and must not grant relief even
        with the scanner green.
        """
        monkeypatch.setenv(
            "_TEST_GH_SCHEDULED_COMMENTS",
            _marker("leaks", body_prefix="[P1] private hostname in a docstring"),
        )
        monkeypatch.setenv("_TEST_GH_CHECK_RUNS", _check_runs(("leak-detector", "success")))
        msg = _gate()
        assert msg and "leaks" in msg


class TestReliefIsScopedToMappedKinds:
    def test_code_review_gets_no_mechanical_relief(self, monkeypatch):
        """Only kinds with a named mechanical counterpart are relievable.

        code-review has no scanner that could stand in for it, so an earlier marker
        plus a green leak-detector must not carry it.
        """
        monkeypatch.setenv("_TEST_REQUIRED_SCHEDULED_REVIEWS", "code-review,leaks")
        monkeypatch.setenv("_TEST_GH_SCHEDULED_COMMENTS", _marker("code-review", "leaks"))
        monkeypatch.setenv("_TEST_GH_CHECK_RUNS", _check_runs(("leak-detector", "success")))
        msg = _gate()
        assert msg and "code-review" in msg
        # leaks WAS relieved, so it must not appear as still-missing.
        assert "leaks" not in msg.split("(required")[0].split("missing at head")[-1]


class TestConclusionReader:
    """_mechanical_scan_conclusion in isolation: None on every doubt."""

    @pytest.mark.parametrize(
        ("payload", "expected"),
        [
            (json.dumps([{"name": "leak-detector", "conclusion": "success"}]), "success"),
            (json.dumps([{"name": "leak-detector", "conclusion": "FAILURE"}]), "failure"),
            (json.dumps([{"name": "leak-detector", "conclusion": None}]), None),
            (json.dumps([{"name": "other", "conclusion": "success"}]), None),
            (json.dumps([]), None),
            (json.dumps({"not": "a list"}), None),
            ("", None),
            ("}{ not json", None),
        ],
    )
    def test_reader(self, monkeypatch, payload, expected):
        monkeypatch.setenv("_TEST_GH_CHECK_RUNS", payload)
        assert _mod._mechanical_scan_conclusion(HEAD, "leak-detector", repo="acme/pub") == expected
