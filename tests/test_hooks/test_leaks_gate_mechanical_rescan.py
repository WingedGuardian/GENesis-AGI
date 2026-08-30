"""The leaks gate honours an ANCESTOR accepted marker when the mechanical scanner
is green at the CURRENT head.

WHY the relief exists. The scheduled routines are not re-run on a push, so on any
multi-push PR the marker sits at the first head and the gate blocks. Measured over
ten recent PRs: every one with commits after its marker was blocked, and the only
escape was `# scheduled-review-override` -- a sigil that verifies NOTHING. Routine
use of an exception valve is worse than a narrower rule.

WHY IT IS SAFE. The two layers catch different leak classes: the mechanical scan
catches literal patterns and runs per-head; the scheduled LLM review catches
inferential leaks. Carrying the LLM verdict forward while REQUIRING the mechanical
one at this exact commit is strictly more checking than the bare override.

THREE PROPERTIES CARRY THAT SAFETY, and each has its own tests below, because
relief granted on the wrong cell silently weakens an irreducible gate:
  * ANCESTRY -- "an earlier head of this PR" is NOT "a sha that differs from
    head". A force-push or rewriting rebase leaves the reviewed commit off the
    branch, so the PR can carry a wholly different tree while the old marker still
    names a real commit. Carrying it would vouch for code no reviewer saw.
  * IDENTITY -- a check is trusted on (name, workflowName), never display name
    alone; a same-named check from another workflow must not stand in for the
    scanner. Same decoy class this file's _ci_identity already documents.
  * HONEST REPORTING -- a carried-forward review must never be rendered as one
    made at head.

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


def _rollup(*, head=HEAD, name="leak-detector", workflow="CI", conclusion="SUCCESS"):
    entries = []
    if name is not None:
        entries.append({"name": name, "workflowName": workflow, "conclusion": conclusion})
    return json.dumps({"headRefOid": head, "statusCheckRollup": entries})


@pytest.fixture(autouse=True)
def _base(monkeypatch):
    monkeypatch.setenv("_TEST_CANONICAL_PUBLIC_REPO", "acme/pub")
    monkeypatch.setenv("_TEST_REQUIRED_SCHEDULED_REVIEWS", "leaks")
    monkeypatch.setenv("_TEST_GH_COMPARE_STATUS", "ahead")  # ancestor unless overridden


def _gate(relief_out=None):
    return _mod._check_scheduled_claude_reviewed_head(
        "1", head_sha=HEAD, repo="acme/pub", relief_out=relief_out
    )


class TestReliefHappyPath:
    def test_ancestor_marker_plus_green_scanner_passes(self, monkeypatch):
        """The whole point: a multi-push PR reviewed once no longer needs an override."""
        monkeypatch.setenv("_TEST_GH_SCHEDULED_COMMENTS", _marker("leaks"))
        monkeypatch.setenv("_TEST_GH_ROLLUP_WITH_HEAD", _rollup())
        assert _gate() is None

    def test_marker_already_at_head_needs_no_relief(self, monkeypatch):
        """CONTROL: the pre-existing pass path works with NO scanner data at all.

        Without this, a bug making relief the ONLY way to pass would still look
        green on the happy-path test above.
        """
        monkeypatch.setenv("_TEST_GH_SCHEDULED_COMMENTS", _marker("leaks", head=HEAD))
        monkeypatch.setenv("_TEST_GH_ROLLUP_WITH_HEAD", "")
        monkeypatch.setenv("_TEST_GH_COMPARE_STATUS", "diverged")
        assert _gate() is None

    def test_relief_is_reported_not_silent(self, monkeypatch):
        """The caller must learn the review was CARRIED, so the report cannot
        render a stale review as 'ok (at head)'."""
        monkeypatch.setenv("_TEST_GH_SCHEDULED_COMMENTS", _marker("leaks"))
        monkeypatch.setenv("_TEST_GH_ROLLUP_WITH_HEAD", _rollup())
        relief: list = []
        assert _gate(relief_out=relief) is None
        assert relief == [("leaks", EARLIER[:12], "leak-detector")]


class TestAncestryIsRequired:
    """A sha that merely DIFFERS from head is not an earlier head of this PR."""

    def test_rewritten_history_blocks(self, monkeypatch):
        """Force-push / rebase: the reviewed commit is not on this branch.

        The scanner is green and the marker is genuine, so ONLY the ancestry
        check stands between a rewritten tree and a carried-forward LLM verdict.
        """
        monkeypatch.setenv("_TEST_GH_SCHEDULED_COMMENTS", _marker("leaks"))
        monkeypatch.setenv("_TEST_GH_ROLLUP_WITH_HEAD", _rollup())
        monkeypatch.setenv("_TEST_GH_COMPARE_STATUS", "diverged")
        msg = _gate()
        assert msg and "leaks" in msg

    def test_unreadable_ancestry_blocks(self, monkeypatch):
        monkeypatch.setenv("_TEST_GH_SCHEDULED_COMMENTS", _marker("leaks"))
        monkeypatch.setenv("_TEST_GH_ROLLUP_WITH_HEAD", _rollup())
        monkeypatch.setenv("_TEST_GH_COMPARE_STATUS", "")
        msg = _gate()
        assert msg and "leaks" in msg

    @pytest.mark.parametrize("status", ["ahead", "behind", "identical", "diverged", "weird"])
    def test_only_ahead_counts(self, monkeypatch, status):
        monkeypatch.setenv("_TEST_GH_COMPARE_STATUS", status)
        assert _mod._sha_is_ancestor(EARLIER, HEAD, repo="acme/pub") is (status == "ahead")


class TestScannerIdentity:
    """(name, workflowName), never the display name alone."""

    def test_same_name_wrong_workflow_blocks(self, monkeypatch):
        """A decoy check with the scanner's name from another workflow must not count."""
        monkeypatch.setenv("_TEST_GH_SCHEDULED_COMMENTS", _marker("leaks"))
        monkeypatch.setenv("_TEST_GH_ROLLUP_WITH_HEAD", _rollup(workflow="Decoy"))
        msg = _gate()
        assert msg and "leaks" in msg

    def test_missing_workflow_name_blocks(self, monkeypatch):
        """A legacy status context / non-Actions app has no workflowName: unidentifiable."""
        monkeypatch.setenv("_TEST_GH_SCHEDULED_COMMENTS", _marker("leaks"))
        monkeypatch.setenv("_TEST_GH_ROLLUP_WITH_HEAD", _rollup(workflow=""))
        msg = _gate()
        assert msg and "leaks" in msg

    def test_rollup_for_a_different_head_blocks(self, monkeypatch):
        """The rollup must describe the commit being decided, or it proves nothing."""
        monkeypatch.setenv("_TEST_GH_SCHEDULED_COMMENTS", _marker("leaks"))
        monkeypatch.setenv("_TEST_GH_ROLLUP_WITH_HEAD", _rollup(head=EARLIER))
        msg = _gate()
        assert msg and "leaks" in msg


class TestReliefFailsClosed:
    def test_scanner_failed_blocks(self, monkeypatch):
        monkeypatch.setenv("_TEST_GH_SCHEDULED_COMMENTS", _marker("leaks"))
        monkeypatch.setenv("_TEST_GH_ROLLUP_WITH_HEAD", _rollup(conclusion="FAILURE"))
        msg = _gate()
        assert msg and "leaks" in msg

    def test_scanner_still_running_blocks(self, monkeypatch):
        monkeypatch.setenv("_TEST_GH_SCHEDULED_COMMENTS", _marker("leaks"))
        monkeypatch.setenv("_TEST_GH_ROLLUP_WITH_HEAD", _rollup(conclusion=None))
        msg = _gate()
        assert msg and "leaks" in msg

    def test_scanner_absent_at_head_blocks(self, monkeypatch):
        monkeypatch.setenv("_TEST_GH_SCHEDULED_COMMENTS", _marker("leaks"))
        monkeypatch.setenv("_TEST_GH_ROLLUP_WITH_HEAD", _rollup(name=None))
        msg = _gate()
        assert msg and "leaks" in msg

    def test_unreadable_rollup_blocks(self, monkeypatch):
        monkeypatch.setenv("_TEST_GH_SCHEDULED_COMMENTS", _marker("leaks"))
        monkeypatch.setenv("_TEST_GH_ROLLUP_WITH_HEAD", "not json at all")
        msg = _gate()
        assert msg and "leaks" in msg

    def test_no_marker_anywhere_blocks_even_when_green(self, monkeypatch):
        """Relief CARRIES a prior review forward; it never manufactures one."""
        monkeypatch.setenv("_TEST_GH_SCHEDULED_COMMENTS", "")
        monkeypatch.setenv("_TEST_GH_ROLLUP_WITH_HEAD", _rollup())
        msg = _gate()
        assert msg and "leaks" in msg

    def test_refused_earlier_marker_does_not_carry(self, monkeypatch):
        """A routine that ran and FOUND something must never be carried forward."""
        monkeypatch.setenv(
            "_TEST_GH_SCHEDULED_COMMENTS",
            _marker("leaks", body_prefix="[P1] private hostname in a docstring"),
        )
        monkeypatch.setenv("_TEST_GH_ROLLUP_WITH_HEAD", _rollup())
        msg = _gate()
        assert msg and "leaks" in msg


class TestReliefIsScopedToMappedKinds:
    def test_code_review_gets_no_mechanical_relief(self, monkeypatch):
        """Only kinds with a named mechanical counterpart are relievable."""
        monkeypatch.setenv("_TEST_REQUIRED_SCHEDULED_REVIEWS", "code-review,leaks")
        monkeypatch.setenv("_TEST_GH_SCHEDULED_COMMENTS", _marker("code-review", "leaks"))
        monkeypatch.setenv("_TEST_GH_ROLLUP_WITH_HEAD", _rollup())
        msg = _gate()
        assert msg and "code-review" in msg


class TestScannerReader:
    """_mechanical_scan_is_green in isolation: False on every doubt."""

    @pytest.mark.parametrize(
        ("payload", "expected"),
        [
            (_rollup(), True),
            (_rollup(conclusion="FAILURE"), False),
            (_rollup(conclusion=None), False),
            (_rollup(workflow="Other"), False),
            (_rollup(name="other-job"), False),
            (_rollup(head=EARLIER), False),
            (_rollup(name=None), False),
            (json.dumps({"headRefOid": HEAD, "statusCheckRollup": None}), False),
            (json.dumps(["not", "a", "dict"]), False),
            ("", False),
            ("}{ not json", False),
        ],
    )
    def test_reader(self, monkeypatch, payload, expected):
        monkeypatch.setenv("_TEST_GH_ROLLUP_WITH_HEAD", payload)
        got = _mod._mechanical_scan_is_green("1", HEAD, "leak-detector", repo="acme/pub")
        assert got is expected
