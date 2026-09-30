"""Latest result per check decides CI (issue #2607).

WHY. ``statusCheckRollup`` keeps every workflow run on the head commit. After a
base-branch fix, a fresh ``pull_request`` run on the SAME head passes, but the earlier
run's FAILURE is still in the rollup, so the gate read ``ci: red`` on a PR whose latest
run is green.

THE RULE, in the one shared primitive ``_latest_result_per_check``: terminal CheckRuns
are grouped by strict identity ``(name, workflowName)``; within a group the entry with
the strictly-latest parsed ``completedAt`` decides. Ties keep every member (any red
stays red). An entry with no identity or no parseable timestamp is always kept and
never supersedes; non-terminal and SKIPPED/NEUTRAL entries are always kept and never
supersede.

NOT A RE-RUN GUARD. GitHub's rollup lists only a re-run's LATEST attempt, so
re-run-until-green is not blocked by this gate, before or after this change (#2624).

Both consumers (``_pr_ci_status`` and ``_mechanical_scan_is_green``) get the rule
through the helper; the agreement tests below lock that.

Network-free: ``_TEST_GH_CI_ROLLUP`` / ``_TEST_GH_ROLLUP_WITH_HEAD`` seams.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.conftest import private_module

_mod = private_module(
    "git_push_guard",
    Path(__file__).resolve().parents[2] / "scripts" / "hooks" / "git_push_guard.py",
)

REPO = "acme/pub"
HEAD = "0cd13afeb51025af5dc7bd24df1ffa57cd2babab"

T1 = "2026-09-29T22:20:11Z"
T2 = "2026-09-30T00:39:24Z"
T3 = "2026-09-30T01:00:00Z"


def _run(conclusion, *, completed_at=None, name="leak-detector", workflow="CI", status="COMPLETED"):
    entry = {
        "__typename": "CheckRun",
        "name": name,
        "workflowName": workflow,
        "status": status,
        "conclusion": conclusion,
    }
    if completed_at is not None:
        entry["completedAt"] = completed_at
    return entry


def _latest(*entries):
    return _mod._latest_result_per_check(list(entries))


def _both(monkeypatch, *entries, scanner=("leak-detector", "CI")):
    """Run the SAME rollup through both consumers and return both verdicts."""
    rollup = list(entries)
    monkeypatch.setenv("_TEST_GH_CI_ROLLUP", json.dumps(rollup))
    monkeypatch.setenv("_TEST_REQUIRED_CI_WORKFLOWS", "CI")
    monkeypatch.setenv(
        "_TEST_GH_ROLLUP_WITH_HEAD",
        json.dumps({"headRefOid": HEAD, "statusCheckRollup": rollup}),
    )
    ci_state, _ = _mod._pr_ci_status("1", repo=REPO)
    green = _mod._mechanical_scan_is_green("1", HEAD, *scanner, repo=REPO)
    return ci_state, green


# ── The primitive ──────────────────────────────────────────────────────────────


class TestLatestWinsPrimitive:
    def test_later_success_supersedes_an_earlier_failure(self):
        fail = _run("FAILURE", completed_at=T1)
        ok = _run("SUCCESS", completed_at=T2)
        assert _latest(fail, ok) == [ok]

    def test_later_failure_overturns_an_earlier_success(self):
        ok = _run("SUCCESS", completed_at=T1)
        fail = _run("FAILURE", completed_at=T2)
        assert _latest(ok, fail) == [fail]

    def test_later_cancel_overturns_an_earlier_success(self):
        """Unchanged from the pre-#2607 cancel rule: SUCCESS then CANCELLED is red."""
        ok = _run("SUCCESS", completed_at=T1)
        cancel = _run("CANCELLED", completed_at=T2)
        assert _latest(ok, cancel) == [cancel]

    def test_timed_out_is_superseded_the_same_way(self):
        timed_out = _run("TIMED_OUT", completed_at=T1)
        ok = _run("SUCCESS", completed_at=T2)
        assert _latest(timed_out, ok) == [ok]

    def test_three_results_only_the_latest_survives(self):
        a = _run("FAILURE", completed_at=T1)
        b = _run("SUCCESS", completed_at=T2)
        c = _run("FAILURE", completed_at=T3)
        assert _latest(a, b, c) == [c]

    def test_input_order_does_not_matter(self):
        fail = _run("FAILURE", completed_at=T1)
        ok = _run("SUCCESS", completed_at=T2)
        assert _latest(ok, fail) == [ok]

    def test_tie_keeps_every_member(self):
        """Second-precision timestamps in the same second are unordered: keep both, so
        the red one still reads red."""
        fail = _run("FAILURE", completed_at=T2)
        ok = _run("SUCCESS", completed_at=T2)
        assert _latest(fail, ok) == [fail, ok]

    def test_tie_at_the_latest_keeps_all_tied_and_drops_older(self):
        old = _run("FAILURE", completed_at=T1)
        tie_ok = _run("SUCCESS", completed_at=T2)
        tie_fail = _run("FAILURE", completed_at=T2)
        assert _latest(old, tie_ok, tie_fail) == [tie_ok, tie_fail]

    def test_tie_across_spellings_is_still_a_tie(self):
        a = _run("FAILURE", completed_at="2026-09-30T00:39:24+00:00")
        b = _run("SUCCESS", completed_at="2026-09-30T00:39:24Z")
        assert _latest(a, b) == [a, b]

    @pytest.mark.parametrize("side", ["older", "newer"])
    def test_missing_timestamp_is_kept_and_never_supersedes(self, side):
        """An entry with no completedAt cannot be ordered: it is always kept, and it
        cannot supersede anything either."""
        no_ts = _run("FAILURE" if side == "older" else "SUCCESS")
        other = _run("SUCCESS" if side == "older" else "FAILURE", completed_at=T2)
        assert _latest(no_ts, other) == [no_ts, other]

    @pytest.mark.parametrize("bad", ["not-a-timestamp", "2026-09-30T00:39:24", ""])
    def test_unparseable_or_naive_timestamp_is_kept(self, bad):
        a = _run("FAILURE", completed_at=bad)
        b = _run("SUCCESS", completed_at=T2)
        assert _latest(a, b) == [a, b]

    def test_different_workflow_name_is_independent(self):
        fail = _run("FAILURE", completed_at=T1)
        decoy = _run("SUCCESS", completed_at=T2, workflow="Decoy")
        assert _latest(fail, decoy) == [fail, decoy]

    def test_different_check_name_is_independent(self):
        fail = _run("FAILURE", completed_at=T1, name="lint")
        other = _run("SUCCESS", completed_at=T2, name="test")
        assert _latest(fail, other) == [fail, other]

    def test_no_identity_is_kept_and_never_supersedes(self):
        """A legacy StatusContext (no workflowName) is never grouped."""
        fail = _run("FAILURE", completed_at=T1)
        legacy = {"context": "leak-detector", "state": "SUCCESS", "completedAt": T2}
        assert _latest(fail, legacy) == [fail, legacy]
        nameless_fail = {
            "workflowName": "CI",
            "status": "COMPLETED",
            "conclusion": "FAILURE",
            "completedAt": T1,
        }
        nameless_ok = {
            "workflowName": "CI",
            "status": "COMPLETED",
            "conclusion": "SUCCESS",
            "completedAt": T2,
        }
        assert _latest(nameless_fail, nameless_ok) == [nameless_fail, nameless_ok]

    @pytest.mark.parametrize("status", ["IN_PROGRESS", "QUEUED", "WAITING"])
    def test_pending_is_kept_and_never_supersedes(self, status):
        fail = _run("FAILURE", completed_at=T1)
        pending = _run(None, completed_at=T2, status=status)
        assert _latest(fail, pending) == [fail, pending]

    def test_non_completed_status_never_supersedes_even_with_a_conclusion(self):
        """The status check is its own layer: an entry whose status is not COMPLETED
        must not supersede even if it carries a (stale or inconsistent) conclusion.
        Without this case the conclusion check alone masks the status check."""
        fail = _run("FAILURE", completed_at=T1)
        inflight = _run("SUCCESS", completed_at=T2, status="IN_PROGRESS")
        assert _latest(fail, inflight) == [fail, inflight]

    @pytest.mark.parametrize(
        "newer",
        [
            {"conclusion": "FOO"},  # unrecognised verdict, no status
            {"conclusion": "success"},  # wrong case, no status
            {"conclusion": "SUCCESS"},  # recognised, but status missing
            {"conclusion": "FOO", "status": "COMPLETED"},  # unrecognised, completed
        ],
    )
    def test_only_a_completed_recognised_verdict_supersedes(self, newer):
        """Found in review: a newer entry the classifier would IGNORE must not erase an
        older FAILURE, or the head reads green on nothing. Only a COMPLETED CheckRun
        whose conclusion is SUCCESS or a recognised red may supersede."""
        fail = _run("FAILURE", completed_at=T1)
        entry = {"name": "leak-detector", "workflowName": "CI", "completedAt": T2, **newer}
        assert _latest(fail, entry) == [fail, entry]

    def test_unrecognised_newer_entry_keeps_ci_red(self, monkeypatch):
        """The same case end to end: before the fix this read `green`."""
        monkeypatch.setenv(
            "_TEST_GH_CI_ROLLUP",
            json.dumps(
                [
                    _run("FAILURE", completed_at=T1, name="test"),
                    {"name": "test", "workflowName": "CI", "conclusion": "FOO", "completedAt": T2},
                    _run("SUCCESS", completed_at=T2, name="lint"),
                ]
            ),
        )
        monkeypatch.setenv("_TEST_REQUIRED_CI_WORKFLOWS", "CI")
        assert _mod._pr_ci_status("1", repo=REPO)[0] == "red"

    def test_completed_without_conclusion_never_supersedes(self):
        fail = _run("FAILURE", completed_at=T1)
        empty = _run(None, completed_at=T2)
        assert _latest(fail, empty) == [fail, empty]

    @pytest.mark.parametrize("skip", ["SKIPPED", "NEUTRAL"])
    def test_skipped_never_supersedes_a_verdict(self, skip):
        """The one exception: a newer run that SKIPPED the job produced no verdict, so
        it must not clear an older run's failure."""
        fail = _run("FAILURE", completed_at=T1)
        skipped = _run(skip, completed_at=T2)
        assert _latest(fail, skipped) == [fail, skipped]

    @pytest.mark.parametrize(
        "conclusion",
        ["CANCELLED", "FAILURE", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE", "STALE"],
    )
    def test_every_red_conclusion_follows_latest_wins(self, conclusion):
        red_old = _run(conclusion, completed_at=T1)
        ok_new = _run("SUCCESS", completed_at=T2)
        assert _latest(red_old, ok_new) == [ok_new]
        ok_old = _run("SUCCESS", completed_at=T1)
        red_new = _run(conclusion, completed_at=T2)
        assert _latest(ok_old, red_new) == [red_new]

    def test_skip_set_is_fully_enumerated(self):
        """DERIVED-SET GUARD for the one exception."""
        test = TestLatestWinsPrimitive.test_skipped_never_supersedes_a_verdict
        parametrized = [m for m in test.pytestmark if m.name == "parametrize"]
        assert set(parametrized[0].args[1]) == set(_mod._CI_SKIP_CONCLUSIONS)
        assert not set(_mod._CI_SKIP_CONCLUSIONS) & set(_mod._CI_ORDERABLE_VERDICTS)

    def test_red_set_is_fully_enumerated(self):
        """DERIVED-SET GUARD: no red conclusion may be added untested."""
        test = TestLatestWinsPrimitive.test_every_red_conclusion_follows_latest_wins
        parametrized = [m for m in test.pytestmark if m.name == "parametrize"]
        assert set(parametrized[0].args[1]) == set(_mod._CI_RED_CONCLUSIONS)

    def test_non_dict_entries_pass_through(self):
        payload = ["junk", None, _run("SUCCESS", completed_at=T2)]
        assert _latest(*payload) == payload

    def test_input_is_not_mutated(self):
        entries = [_run("FAILURE", completed_at=T1), _run("SUCCESS", completed_at=T2)]
        before = json.dumps(entries)
        _mod._latest_result_per_check(entries)
        assert json.dumps(entries) == before


# ── Both consumers, one payload ─────────────────────────────────────────────────


class TestBothConsumersAgree:
    def test_later_success_is_green_to_both(self, monkeypatch):
        assert _both(
            monkeypatch, _run("FAILURE", completed_at=T1), _run("SUCCESS", completed_at=T2)
        ) == ("green", True)

    def test_later_failure_is_red_to_both(self, monkeypatch):
        assert _both(
            monkeypatch, _run("SUCCESS", completed_at=T1), _run("FAILURE", completed_at=T2)
        ) == ("red", False)

    def test_tie_with_a_red_member_is_red_to_both(self, monkeypatch):
        assert _both(
            monkeypatch, _run("FAILURE", completed_at=T2), _run("SUCCESS", completed_at=T2)
        ) == ("red", False)

    def test_missing_timestamp_failure_is_red_to_both(self, monkeypatch):
        assert _both(monkeypatch, _run("FAILURE"), _run("SUCCESS", completed_at=T2)) == (
            "red",
            False,
        )

    def test_different_workflow_is_red_to_both(self, monkeypatch):
        assert _both(
            monkeypatch,
            _run("FAILURE", completed_at=T1),
            _run("SUCCESS", completed_at=T2, workflow="Decoy"),
        ) == ("red", False)

    def test_pending_other_check_is_pending_not_green(self, monkeypatch):
        state, green = _both(
            monkeypatch,
            _run("FAILURE", completed_at=T1),
            _run("SUCCESS", completed_at=T2),
            _run(None, status="IN_PROGRESS", name="lint"),
        )
        assert (state, green) == ("pending", True)

    def test_inflight_rerun_of_the_same_check_does_not_clear_its_failure(self, monkeypatch):
        state, green = _both(
            monkeypatch,
            _run("FAILURE", completed_at=T1),
            _run(None, status="IN_PROGRESS"),
        )
        assert state != "green" and green is False

    def test_newer_skip_does_not_clear_a_failure_for_either_consumer(self, monkeypatch):
        assert _both(
            monkeypatch, _run("FAILURE", completed_at=T1), _run("SKIPPED", completed_at=T2)
        ) == ("red", False)


# ── Characterization: the real PR #2484 shape ───────────────────────────────────


class TestPr2484Characterization:
    """ACCEPTANCE BAR. PR #2484's head carried three `pull_request` runs of CI.
    lint and test FAILED in runs 36638934761 and 36647810311 (a broken base), then
    PASSED in 36651350733 after the base was fixed. Every other CI job passed in all
    three. Before #2607 the gate read `ci: red (lint, test)`. The completedAt values
    are the real ones; detailsUrl carries the real run ids on a synthetic slug."""

    R1, R2, R3 = 36638934761, 36647810311, 36651350733

    ROWS = [
        ("lint", R1, "FAILURE", "2026-09-29T22:20:11Z"),
        ("lint", R2, "FAILURE", "2026-09-29T23:56:53Z"),
        ("lint", R3, "SUCCESS", "2026-09-30T00:39:24Z"),
        ("test", R1, "FAILURE", "2026-09-29T23:01:31Z"),
        ("test", R2, "FAILURE", "2026-09-30T00:29:21Z"),
        ("test", R3, "SUCCESS", "2026-09-30T01:26:58Z"),
        ("leak-detector", R1, "SUCCESS", "2026-09-29T22:21:05Z"),
        ("leak-detector", R2, "SUCCESS", "2026-09-29T23:57:55Z"),
        ("leak-detector", R3, "SUCCESS", "2026-09-30T00:40:15Z"),
        ("migration-check", R1, "SUCCESS", "2026-09-29T22:20:15Z"),
        ("migration-check", R2, "SUCCESS", "2026-09-29T23:56:51Z"),
        ("migration-check", R3, "SUCCESS", "2026-09-30T00:39:13Z"),
    ]

    def _rollup(self, rows):
        out = []
        for n, run, c, at in rows:
            e = _run(c, completed_at=at, name=n)
            e["detailsUrl"] = f"https://github.com/{REPO}/actions/runs/{run}/job/1"
            out.append(e)
        return out

    def test_real_shape_reads_green(self, monkeypatch):
        assert _both(monkeypatch, *self._rollup(self.ROWS)) == ("green", True)

    def test_real_shape_reads_red_if_the_last_run_had_failed(self, monkeypatch):
        """CONTROL: flip run 3's `test` to FAILURE — the latest result decides, so red."""
        rows = [
            (n, run, "FAILURE" if (n == "test" and run == self.R3) else c, at)
            for n, run, c, at in self.ROWS
        ]
        state, _ = _both(monkeypatch, *self._rollup(rows))
        assert state == "red"
