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

    def test_same_name_from_another_REQUIRED_workflow_blocks(self, monkeypatch):
        """Round-4 finding, now pinned: membership in the required-CI SET is not
        identity. The pre-pin code PASSED this shape (measured by the audit)."""
        monkeypatch.setenv("_TEST_REQUIRED_CI_WORKFLOWS", "CI,Nightly")
        monkeypatch.setenv("_TEST_GH_SCHEDULED_COMMENTS", _marker("leaks"))
        monkeypatch.setenv("_TEST_GH_ROLLUP_WITH_HEAD", _rollup(workflow="Nightly"))
        msg = _gate()
        assert msg and "not green at this head" in msg

    def test_empty_workflow_pin_fails_closed(self, monkeypatch):
        monkeypatch.setenv("_TEST_GH_ROLLUP_WITH_HEAD", _rollup())
        assert _mod._mechanical_scan_is_green("1", HEAD, "leak-detector", "", repo="acme/pub") is False

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
        got = _mod._mechanical_scan_is_green("1", HEAD, "leak-detector", "CI", repo="acme/pub")
        assert got is expected


REFUSED_BODY = "[P1] inferential leak: a docstring naming a home town and employer"
INTERMEDIATE = "2222222222222222222222222222222222222222"


def _markers(*entries):
    """Several owner-authored marker comments, oldest first (API order)."""
    rows = []
    for head, kinds, prefix in entries:
        body = prefix + "\n" + "\n".join(
            f"<!-- genesis-scheduled-review: head={head} kind={k} -->" for k in kinds
        )
        rows.append(json.dumps({"login": "owner", "author_association": "OWNER", "body": body}))
    return "\n".join(rows)


class TestAnyRefusalDenies:
    """THE RULE: a refusal for the kind, anywhere in the PR, denies relief.

    The mechanical scanner cannot see inferential leaks by construction, so carrying
    an older clean review past a refusal would convert a DETECTED leak into a merge.
    The previous predicate tried to establish that every refusal PREDATED the carried
    review via ancestry compares; four review rounds found four ways that
    reconstruction was wrong. The membership test replaces all of it. The owner's
    chronology already ran inside the scan: a refusal answered by a clean verdict AT
    ITS OWN HEAD never reaches `rejected`, so what lands here is a refusal the owner
    never retracted at the commit it was made about.
    """

    def test_refused_at_current_head_blocks(self, monkeypatch):
        monkeypatch.setenv(
            "_TEST_GH_SCHEDULED_COMMENTS",
            _markers(
                (EARLIER, ["leaks"], "scheduled review done. VERDICT: PASS"),
                (HEAD, ["leaks"], REFUSED_BODY),
            ),
        )
        monkeypatch.setenv("_TEST_GH_ROLLUP_WITH_HEAD", _rollup())
        msg = _gate()
        assert msg and "leaks" in msg

    def test_refused_between_the_carried_review_and_head_blocks(self, monkeypatch):
        monkeypatch.setenv(
            "_TEST_GH_SCHEDULED_COMMENTS",
            _markers(
                (EARLIER, ["leaks"], "scheduled review done. VERDICT: PASS"),
                (INTERMEDIATE, ["leaks"], REFUSED_BODY),
            ),
        )
        monkeypatch.setenv("_TEST_GH_ROLLUP_WITH_HEAD", _rollup())
        msg = _gate()
        assert msg and "leaks" in msg

    def test_a_refusal_that_predates_the_accepted_review_still_denies(self, monkeypatch):
        """DELIBERATE, decided 2026-08-30: the head axis is not a time axis.

        A clean review of yesterday's code says nothing about today's, so a LATER
        acceptance at an OLDER head must never outrank a refusal. Cross-head resolution
        may only ever ADD acceptance, never remove a refusal. The previous version of
        this test asserted the opposite ("answered by a later review still relieves");
        that was the predicate the redesign deleted. Measured cost on the 30 most
        recent marker-carrying PRs: zero relief lost -- every observed refusal was
        superseded at its own head, false, or followed by a clean marker at the final
        head. The fallback for the case that does occur is the override plus a human
        reading a leak finding, which is the right outcome.
        """
        monkeypatch.setenv(
            "_TEST_GH_SCHEDULED_COMMENTS",
            _markers(
                (INTERMEDIATE, ["leaks"], REFUSED_BODY),
                (EARLIER, ["leaks"], "re-reviewed after the fix. VERDICT: PASS"),
            ),
        )
        monkeypatch.setenv("_TEST_GH_ROLLUP_WITH_HEAD", _rollup())
        msg = _gate()
        assert msg and "leaks" in msg
        assert "REFUSED in this PR" in msg, "the reason must name the refusal, not the scanner"

    def test_no_ancestry_compare_is_spent_on_a_refusal(self, monkeypatch):
        """The membership test costs no network: with a refusal present, relief is
        denied before any compare runs -- even when every compare would be
        UNREADABLE. (The old predicate walked the refusals with per-refusal compares
        and could exhaust the shared merge deadline doing it.)"""
        monkeypatch.setenv(
            "_TEST_GH_SCHEDULED_COMMENTS",
            _markers(
                (EARLIER, ["leaks"], "scheduled review done. VERDICT: PASS"),
                (INTERMEDIATE, ["leaks"], REFUSED_BODY),
            ),
        )
        monkeypatch.setenv("_TEST_GH_ROLLUP_WITH_HEAD", _rollup())
        monkeypatch.setenv("_TEST_GH_COMPARE_STATUS", "")  # every compare unreadable
        msg = _gate()
        assert msg and "REFUSED in this PR" in msg
        assert "time available" not in msg, "a refusal must be the stated cause, not the deadline"


def _stamped(*entries):
    """Owner marker rows WITH timestamps, so the scan's tie rule can engage (rows without
    a stamp keep list order and cannot tie)."""
    rows = []
    for head, kinds, prefix, stamp in entries:
        body = prefix + "\n" + "\n".join(
            f"<!-- genesis-scheduled-review: head={head} kind={k} -->" for k in kinds
        )
        rows.append(json.dumps({
            "login": "owner", "author_association": "OWNER", "body": body, "stamp": stamp,
        }))
    return "\n".join(rows)


class TestBlockingResidueDenies:
    """The path an adversarial audit REPRODUCED on the entry point (2026-08-30): a
    blocking finding the scan files under `unusable` rather than `rejected` must deny
    relief exactly as a refusal does. Both shapes below granted relief before the scan
    exposed `blocking_residue`."""

    def test_blocking_body_under_a_short_sha_at_head_denies(self, monkeypatch):
        """A [P1] under a 12-char head= is a producer fault the scan records as seen
        live. It is unattributable to a head, so it is residue under "" and denies."""
        monkeypatch.setenv(
            "_TEST_GH_SCHEDULED_COMMENTS",
            _markers(
                (EARLIER, ["leaks"], "scheduled review done. VERDICT: PASS"),
                (HEAD[:12], ["leaks"], REFUSED_BODY),
            ),
        )
        monkeypatch.setenv("_TEST_GH_ROLLUP_WITH_HEAD", _rollup())
        msg = _gate()
        assert msg and "leaks" in msg
        assert "could not be credited" in msg, "the reason must name the residue, not the scanner"

    def test_blocking_body_under_an_unknown_kind_denies_every_kind(self, monkeypatch):
        monkeypatch.setenv(
            "_TEST_GH_SCHEDULED_COMMENTS",
            _markers(
                (EARLIER, ["leaks"], "scheduled review done. VERDICT: PASS"),
                (HEAD, ["leak"], REFUSED_BODY),  # singular typo: no known kind
            ),
        )
        monkeypatch.setenv("_TEST_GH_ROLLUP_WITH_HEAD", _rollup())
        msg = _gate()
        assert msg and "could not be credited" in msg

    def test_short_sha_with_a_valid_but_UNKNOWN_kind_denies(self, monkeypatch):
        """Codex round 6, reproduced through the gate: `head=<short> kind=leak` parses a
        syntactically valid kind that names no real review. Filing residue under "leak"
        put the blocking finding somewhere no required kind ever looks, and relief
        carried past it. An unknown kind is unattributable -> "*" -> denies every kind."""
        monkeypatch.setenv(
            "_TEST_GH_SCHEDULED_COMMENTS",
            _markers(
                (EARLIER, ["leaks"], "scheduled review done. VERDICT: PASS"),
                (HEAD[:12], ["leak"], REFUSED_BODY),
            ),
        )
        monkeypatch.setenv("_TEST_GH_ROLLUP_WITH_HEAD", _rollup())
        msg = _gate()
        assert msg and "could not be credited" in msg

    def test_short_sha_with_a_KNOWN_other_kind_denies_only_that_kind(self, monkeypatch):
        """CONTROL for the fix above: a KNOWN kind is still retained, so residue for
        `code-review` does not deny `leaks`. Without this the fix could be "always *",
        which would make every malformed blocking marker deny every kind forever."""
        monkeypatch.setenv("_TEST_GH_SCHEDULED_COMMENTS",
            _markers(
                (EARLIER, ["leaks"], "scheduled review done. VERDICT: PASS"),
                (HEAD[:12], ["code-review"], REFUSED_BODY),
            ),
        )
        monkeypatch.setenv("_TEST_GH_ROLLUP_WITH_HEAD", _rollup())
        assert _gate() is None

    def test_same_timestamp_tie_at_head_denies(self, monkeypatch):
        """A clean verdict and a blocking finding at HEAD with the SAME stamp land in
        neither verdict map by design; the tie is residue at HEAD and denies."""
        monkeypatch.setenv(
            "_TEST_GH_SCHEDULED_COMMENTS",
            _stamped(
                (EARLIER, ["leaks"], "scheduled review done. VERDICT: PASS", "2026-08-30T10:00:00Z"),
                (HEAD, ["leaks"], "re-run. VERDICT: PASS", "2026-08-30T12:00:00Z"),
                (HEAD, ["leaks"], REFUSED_BODY, "2026-08-30T12:00:00Z"),
            ),
        )
        monkeypatch.setenv("_TEST_GH_ROLLUP_WITH_HEAD", _rollup())
        msg = _gate()
        assert msg and "could not be credited" in msg

    def test_benign_unusable_rows_do_not_deny(self, monkeypatch):
        """CONTROL: a short-sha marker with a CLEAN body is plain `unusable`, never
        residue -- 7 of 40 recent PRs carry rows like it, and they must still relieve."""
        monkeypatch.setenv(
            "_TEST_GH_SCHEDULED_COMMENTS",
            _markers(
                (EARLIER, ["leaks"], "scheduled review done. VERDICT: PASS"),
                (HEAD[:12], ["leaks"], "re-run. PII scan: CLEAN"),
            ),
        )
        monkeypatch.setenv("_TEST_GH_ROLLUP_WITH_HEAD", _rollup())
        assert _gate() is None


class TestDuplicateRollupEntries:
    """One head can carry several runs of one job; order is not a guarantee."""

    @pytest.mark.parametrize("order", ["success_first", "failure_first"])
    def test_a_contradicting_rerun_blocks_in_either_order(self, monkeypatch, order):
        entries = [
            {"name": "leak-detector", "workflowName": "CI", "conclusion": "SUCCESS"},
            {"name": "leak-detector", "workflowName": "CI", "conclusion": "FAILURE"},
        ]
        if order == "failure_first":
            entries.reverse()
        monkeypatch.setenv("_TEST_GH_SCHEDULED_COMMENTS", _marker("leaks"))
        monkeypatch.setenv(
            "_TEST_GH_ROLLUP_WITH_HEAD",
            json.dumps({"headRefOid": HEAD, "statusCheckRollup": entries}),
        )
        msg = _gate()
        assert msg and "leaks" in msg

    def test_two_agreeing_successes_still_relieve(self, monkeypatch):
        entries = [
            {"name": "leak-detector", "workflowName": "CI", "conclusion": "SUCCESS"},
            {"name": "leak-detector", "workflowName": "CI", "conclusion": "SUCCESS"},
        ]
        monkeypatch.setenv("_TEST_GH_SCHEDULED_COMMENTS", _marker("leaks"))
        monkeypatch.setenv(
            "_TEST_GH_ROLLUP_WITH_HEAD",
            json.dumps({"headRefOid": HEAD, "statusCheckRollup": entries}),
        )
        assert _gate() is None


class TestCandidateSelection:
    """Any accepted ancestor will do. Nothing rests on WHICH one is carried: the safety
    argument is the per-head mechanical scan, never the age of the carried review (the
    previous code took the last candidate and called that "newest", a claim the SHA-keyed
    map cannot support). So an unreadable compare is "I do not know", not "no": it is
    recorded and the next candidate is tried; only when no candidate verifies does the
    unknown decide -- closed."""

    def test_multiple_accepted_markers_relieve(self, monkeypatch):
        """The load-bearing case: about a third of recent PRs carry MORE than one accepted
        leaks marker. Both are ancestors; relief must not depend on picking one."""
        monkeypatch.setenv(
            "_TEST_GH_SCHEDULED_COMMENTS",
            _markers(
                (INTERMEDIATE, ["leaks"], "older. VERDICT: PASS"),
                (EARLIER, ["leaks"], "newer. VERDICT: PASS"),
            ),
        )
        monkeypatch.setenv("_TEST_GH_ROLLUP_WITH_HEAD", _rollup())
        relief: list = []
        assert _gate(relief_out=relief) is None
        assert relief and relief[0][0] == "leaks"

    def test_one_unreadable_candidate_does_not_block_when_another_verifies(self, monkeypatch):
        monkeypatch.setenv(
            "_TEST_GH_SCHEDULED_COMMENTS",
            _markers(
                (INTERMEDIATE, ["leaks"], "first in scan order. VERDICT: PASS"),
                (EARLIER, ["leaks"], "second. VERDICT: PASS"),
            ),
        )
        monkeypatch.setenv("_TEST_GH_ROLLUP_WITH_HEAD", _rollup())
        # INTERMEDIATE's compare is unreadable (unspecified); EARLIER's is 'ahead'.
        monkeypatch.setenv(
            "_TEST_GH_COMPARE_STATUS",
            json.dumps({f"{EARLIER}...{HEAD}": "ahead"}),
        )
        assert _gate() is None

    def test_all_unreadable_fails_closed(self, monkeypatch):
        """When NOTHING can be proven the unknown decides, and it decides closed --
        never a silent fall-through to relief."""
        monkeypatch.setenv(
            "_TEST_GH_SCHEDULED_COMMENTS",
            _markers(
                (INTERMEDIATE, ["leaks"], "older. VERDICT: PASS"),
                (EARLIER, ["leaks"], "newer. VERDICT: PASS"),
            ),
        )
        monkeypatch.setenv("_TEST_GH_ROLLUP_WITH_HEAD", _rollup())
        monkeypatch.setenv("_TEST_GH_COMPARE_STATUS", json.dumps({}))
        msg = _gate()
        assert msg and "leaks" in msg
        assert "time available" in msg

    def test_off_branch_plus_unreadable_still_fails_closed(self, monkeypatch):
        """A candidate that is provably NOT an ancestor is skipped (that is a real
        'no'); if the only other candidate is unreadable, the unknown still wins."""
        monkeypatch.setenv(
            "_TEST_GH_SCHEDULED_COMMENTS",
            _markers(
                (INTERMEDIATE, ["leaks"], "rewritten away. VERDICT: PASS"),
                (EARLIER, ["leaks"], "unreadable. VERDICT: PASS"),
            ),
        )
        monkeypatch.setenv("_TEST_GH_ROLLUP_WITH_HEAD", _rollup())
        monkeypatch.setenv(
            "_TEST_GH_COMPARE_STATUS",
            json.dumps({f"{INTERMEDIATE}...{HEAD}": "diverged"}),
        )
        msg = _gate()
        assert msg and "time available" in msg


class TestSameShaCollision:
    def test_accepted_and_refused_on_the_same_commit_vetoes(self, monkeypatch):
        """One commit carrying both verdicts is resolved by the SCAN, not by us.

        The routine can run twice on one head -- clean, then a re-run that finds an
        inferential leak. The scan resolves that by the owner's latest DECISIVE
        statement (rows carry timestamps; the test seam keeps list order), so the later
        refusal governs and lands in `rejected`; the membership test then denies. The
        relief code carries NO same-commit veto of its own any more -- two answers to
        one question was exactly the class the redesign deleted.
        """
        monkeypatch.setenv(
            "_TEST_GH_SCHEDULED_COMMENTS",
            _markers(
                (EARLIER, ["leaks"], "scheduled review done. VERDICT: PASS"),
                (EARLIER, ["leaks"], REFUSED_BODY),
            ),
        )
        monkeypatch.setenv("_TEST_GH_ROLLUP_WITH_HEAD", _rollup())
        msg = _gate()
        assert msg and "leaks" in msg


class TestMergeDeadline:
    def test_an_exhausted_deadline_blocks_rather_than_walking_on(self, monkeypatch):
        """Relief must not spend the shared merge budget.

        The merge gates run sequentially under ONE deadline; each ancestry check is a
        network call. A PR with many markers could walk that budget to zero, and an
        overrun gets the whole hook SIGKILLed -- which fails toward "the tool runs"
        and disengages the ENTIRE gate stack. Relief being the thing that spends it
        would be a bypass of every gate, not just this one.
        """
        monkeypatch.setenv("_TEST_GH_SCHEDULED_COMMENTS", _marker("leaks"))
        monkeypatch.setenv("_TEST_GH_ROLLUP_WITH_HEAD", _rollup())
        monkeypatch.setattr(_mod, "_merge_deadline", _mod.time.monotonic() - 1)
        msg = _gate()
        assert msg and "leaks" in msg
        assert "time available" in msg, "the block must name the deadline as the cause"


class TestBlockMessageNamesTheRealRemedy:
    """A failed relief has an observed cause; the message must give ITS remedy."""

    def test_pending_scanner_is_not_reported_as_a_stale_marker(self, monkeypatch):
        monkeypatch.setenv("_TEST_GH_SCHEDULED_COMMENTS", _marker("leaks"))
        monkeypatch.setenv("_TEST_GH_ROLLUP_WITH_HEAD", _rollup(conclusion=None))
        msg = _gate()
        assert msg and "leak-detector" in msg, "the message must name the scanner"
        assert "not green at this head" in msg
        # The scanner cause is reported ALONGSIDE the generic partition, not instead
        # of it -- that partition is hard-won and its own tests guard it. What matters
        # is that the specific, actionable cause is present and named, and that it
        # comes FIRST so it is read before the generic advice.
        assert msg.index("leak-detector") < msg.index("kind=leaks") if "kind=leaks" in msg else True

    def test_refused_at_head_says_so_rather_than_blaming_the_scanner(self, monkeypatch):
        monkeypatch.setenv(
            "_TEST_GH_SCHEDULED_COMMENTS",
            _markers(
                (EARLIER, ["leaks"], "scheduled review done. VERDICT: PASS"),
                (HEAD, ["leaks"], REFUSED_BODY),
            ),
        )
        monkeypatch.setenv("_TEST_GH_ROLLUP_WITH_HEAD", _rollup())
        msg = _gate()
        assert msg and "REFUSED in this PR" in msg


# ══════════════════════════════════════════════════════════════════════════
# The CLASS, enumerated: blocking evidence must never be filed where the
# relief decision does not read it
# ══════════════════════════════════════════════════════════════════════════
_HEADS = {"valid": HEAD, "short": HEAD[:12], "empty": "", "absent": None}
_KINDS = {"required": "leaks", "known-other": "code-review", "unknown": "leak", "absent": None}
_BODIES = {"blocking": REFUSED_BODY, "clean": "scheduled review done. VERDICT: PASS"}
_STATES = {"live": None, "dismissed": "DISMISSED", "pending": "PENDING"}
_AUTHORS = {"owner": ("owner", "OWNER"), "stranger": ("someone", "NONE")}


def _cell_marker(head, kind):
    parts = []
    if head is not None:
        parts.append(f"head={head}")
    if kind is not None:
        parts.append(f"kind={kind}")
    return "<!-- genesis-scheduled-review: " + " ".join(parts) + " -->"


@pytest.mark.parametrize("author", sorted(_AUTHORS))
@pytest.mark.parametrize("state", sorted(_STATES))
@pytest.mark.parametrize("body", sorted(_BODIES))
@pytest.mark.parametrize("kind", sorted(_KINDS))
@pytest.mark.parametrize("head", sorted(_HEADS))
def test_matrix_blocking_evidence_is_never_filed_out_of_reach(
    head, kind, body, state, author, monkeypatch
):
    """Two review rounds found the SAME class one branch apart: a blocking finding
    recorded somewhere the relief decision does not read (round 5: the `unusable`
    bucket; round 6: residue filed under an unknown kind). Two instances is the
    signal to enumerate rather than patch a third time.

    The axes below are the ones that decide WHERE the scan files a marker. The
    invariant: a LIVE, OWNER-authored, BLOCKING marker denies relief for the kind it
    names, and for EVERY kind when it names none that exists — an unattributable
    finding is evidence about all reviews, not one.

    Deliberately NOT asserted, each with the scan's own reason (an unasserted cell
    and a hole look identical, so they are named here):
      * author=stranger — a non-owner comment is not the owner's routine speaking;
        the scan drops it before any verdict (`login != owner`).
      * state=dismissed — a dismissed review no longer vouches for anything, and
        dismissal is an authoritative act by someone with permission.
      * state=pending — an unpublished draft never ran publicly; the scan states it
        is read like any other block once submitted.
    """
    login, assoc = _AUTHORS[author]
    row = {"login": login, "author_association": assoc,
           "body": _BODIES[body] + "\n" + _cell_marker(_HEADS[head], _KINDS[kind])}
    if _STATES[state]:
        row["state"] = _STATES[state]
    monkeypatch.setenv("_TEST_GH_SCHEDULED_COMMENTS", "\n".join([
        json.dumps({"login": "owner", "author_association": "OWNER",
                    "body": "scheduled review done. VERDICT: PASS\n"
                            + _cell_marker(EARLIER, "leaks")}),
        json.dumps(row),
    ]))
    monkeypatch.setenv("_TEST_GH_ROLLUP_WITH_HEAD", _rollup())

    denied = _gate() is not None
    live_owner_blocking = body == "blocking" and state == "live" and author == "owner"
    if live_owner_blocking and _KINDS[kind] != "code-review":
        assert denied, (
            f"blocking finding filed out of reach: head={head} kind={kind} — relief was "
            f"granted over a live owner finding"
        )
    elif live_owner_blocking:
        assert not denied, (
            "over-correction: a code-review finding must not deny the leaks gate"
        )
