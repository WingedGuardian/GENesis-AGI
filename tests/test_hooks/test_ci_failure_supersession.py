"""A FAILURE / TIMED_OUT check-run is superseded ONLY by a strictly-later SUCCESS of the
same ``(name, workflowName)`` from a DIFFERENT, NEWER workflow run (issue #2607).

WHY the relief exists. ``statusCheckRollup`` keeps every workflow run on the head
commit. After a base-branch fix, a fresh ``pull_request`` run on the SAME head passes,
but the earlier run's FAILURE is still in the rollup, so the gate read ``ci: red`` on a
PR whose latest run is green.

WHY it stays narrow. The merge gate forces ``--admin``, so it is the only CI
enforcement. A re-attempt of the same run (``gh run rerun``) keeps its run id and
replays the same inputs; letting that turn a failure green is "re-run until green".
NOTE: the live rollup keeps only each run's LATEST attempt, so a passing re-attempt
replaces its own failure before this helper sees it. The same-run cases below are
UNIT properties of the rule (a failure still present is not cleared by its own or an
older run), not a claim that the gate refuses `gh run rerun`.
So the superseding success must come from a DIFFERENT, NEWER run (larger run id),
parsed strictly from the CheckRun's ``detailsUrl`` (github.com, this repo's slug, a
numeric run id). Anything
that cannot be proven — no run id, a foreign repo, no timestamp, a tie, a different
workflow — stays red.

Both consumers (``_pr_ci_status`` and ``_mechanical_scan_is_green``) get this through
the ONE shared primitive, ``_drop_superseded_cancels``; the agreement tests below lock
that.

Network-free: ``_TEST_GH_CI_ROLLUP`` / ``_TEST_GH_ROLLUP_WITH_HEAD`` /
``_TEST_GH_DERIVED_REPO`` seams.
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

FAIL_AT = "2026-09-29T22:20:11Z"
PASS_AT = "2026-09-30T00:39:24Z"
LATER_AT = "2026-09-30T01:00:00Z"

RUN_A = 36638934761
RUN_B = 36651350733
RUN_C = 36660000001
# Run id used inside the malformed-URL cells: strictly BETWEEN RUN_A and RUN_B, so on
# the failure side it is older than RUN_B and on the success side newer than RUN_A. A
# mutation that let a spoofed URL parse is then not masked by the newer-run rule.
RUN_G = 36645000009
assert RUN_A < RUN_G < RUN_B

_job_counter = iter(range(109600000000, 109700000000))


def _url(run_id, *, slug=REPO):
    return f"https://github.com/{slug}/actions/runs/{run_id}/job/{next(_job_counter)}"


def _run(
    conclusion,
    run_id=None,
    *,
    completed_at=None,
    name="leak-detector",
    workflow="CI",
    slug=REPO,
    details_url=None,
):
    entry = {
        "__typename": "CheckRun",
        "name": name,
        "workflowName": workflow,
        "status": "COMPLETED",
        "conclusion": conclusion,
    }
    if completed_at is not None:
        entry["completedAt"] = completed_at
    if details_url is not None:
        entry["detailsUrl"] = details_url
    elif run_id is not None:
        entry["detailsUrl"] = _url(run_id, slug=slug)
    return entry


def _drop(*entries, repo=REPO):
    return _mod._drop_superseded_cancels(list(entries), repo=repo)


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


class TestFailureSupersessionPrimitive:
    def test_failure_then_later_success_from_another_run_is_dropped(self):
        fail = _run("FAILURE", RUN_A, completed_at=FAIL_AT)
        ok = _run("SUCCESS", RUN_B, completed_at=PASS_AT)
        assert _drop(fail, ok) == [ok]

    def test_timed_out_is_superseded_the_same_way(self):
        timed_out = _run("TIMED_OUT", RUN_A, completed_at=FAIL_AT)
        ok = _run("SUCCESS", RUN_B, completed_at=PASS_AT)
        assert _drop(timed_out, ok) == [ok]

    def test_same_run_id_reattempt_stays_red(self):
        """Unit property: a success from the SAME run id never clears a failure that is
        still present. (The live rollup drops a re-attempted run's earlier attempts,
        so this pair only reaches the helper if both attempts are listed.)"""
        fail = _run("FAILURE", RUN_A, completed_at=FAIL_AT)
        ok = _run("SUCCESS", RUN_A, completed_at=PASS_AT)
        assert _drop(fail, ok) == [fail, ok]

    def test_tie_stays_red(self):
        fail = _run("FAILURE", RUN_A, completed_at=PASS_AT)
        ok = _run("SUCCESS", RUN_B, completed_at=PASS_AT)
        assert _drop(fail, ok) == [fail, ok]

    def test_success_before_failure_stays_red(self):
        ok = _run("SUCCESS", RUN_A, completed_at=FAIL_AT)
        fail = _run("FAILURE", RUN_B, completed_at=PASS_AT)
        assert _drop(ok, fail) == [ok, fail]

    def test_later_failure_from_a_third_run_stays_red(self):
        """A success supersedes the failures BEFORE it, never one after it."""
        first = _run("FAILURE", RUN_A, completed_at=FAIL_AT)
        ok = _run("SUCCESS", RUN_B, completed_at=PASS_AT)
        last = _run("FAILURE", RUN_C, completed_at=LATER_AT)
        assert _drop(first, ok, last) == [ok, last]

    def test_reattempt_success_does_not_hide_an_earlier_cross_run_success(self):
        """Run A fails, run B passes, then A is re-attempted and passes. B is the
        proof, so A's failure is superseded; A's own re-attempt is irrelevant."""
        fail = _run("FAILURE", RUN_A, completed_at=FAIL_AT)
        ok_b = _run("SUCCESS", RUN_B, completed_at=PASS_AT)
        ok_a = _run("SUCCESS", RUN_A, completed_at=LATER_AT)
        assert _drop(fail, ok_b, ok_a) == [ok_b, ok_a]

    def test_two_runs_cannot_cross_cover_via_reattempts(self):
        """Two failing runs, each then re-attempted to green. Every pass is a same-run
        re-attempt, so BOTH failures must stay red. Under a bare 'different run id'
        test, run A's re-attempt covers run B's failure and vice versa, and the gate
        reads green: re-run-until-green by another route. The superseding run must be
        NEWER than the failing one."""
        fail_a = _run("FAILURE", RUN_A, completed_at="2026-09-29T22:00:00Z")
        fail_b = _run("FAILURE", RUN_B, completed_at="2026-09-29T22:10:00Z")
        ok_a = _run("SUCCESS", RUN_A, completed_at="2026-09-29T22:20:00Z")
        ok_b = _run("SUCCESS", RUN_B, completed_at="2026-09-29T22:30:00Z")
        kept = _drop(fail_a, fail_b, ok_a, ok_b)
        assert fail_b in kept, "run B's failure was cleared by a re-attempt of an OLDER run"

    def test_reattempt_of_an_older_run_does_not_clear_a_newer_failure(self):
        """The narrowest form of the cross-cover: an OLDER run's success, completing
        after a NEWER run failed, is not new evidence about the newer run."""
        fail_new = _run("FAILURE", RUN_B, completed_at=FAIL_AT)
        ok_old = _run("SUCCESS", RUN_A, completed_at=PASS_AT)
        assert _drop(fail_new, ok_old) == [fail_new, ok_old]

    def test_different_workflow_name_stays_red(self):
        fail = _run("FAILURE", RUN_A, completed_at=FAIL_AT)
        decoy = _run("SUCCESS", RUN_B, completed_at=PASS_AT, workflow="Decoy")
        assert _drop(fail, decoy) == [fail, decoy]

    def test_different_check_name_stays_red(self):
        fail = _run("FAILURE", RUN_A, completed_at=FAIL_AT, name="lint")
        other = _run("SUCCESS", RUN_B, completed_at=PASS_AT, name="test")
        assert _drop(fail, other) == [fail, other]

    @pytest.mark.parametrize("side", ["failure", "success"])
    def test_missing_completed_at_stays_red(self, side):
        fail = _run("FAILURE", RUN_A, completed_at=None if side == "failure" else FAIL_AT)
        ok = _run("SUCCESS", RUN_B, completed_at=None if side == "success" else PASS_AT)
        assert _drop(fail, ok) == [fail, ok]

    @pytest.mark.parametrize("side", ["failure", "success"])
    def test_missing_details_url_stays_red(self, side):
        fail = _run("FAILURE", RUN_A if side != "failure" else None, completed_at=FAIL_AT)
        ok = _run("SUCCESS", RUN_B if side != "success" else None, completed_at=PASS_AT)
        assert _drop(fail, ok) == [fail, ok]

    @pytest.mark.parametrize(
        "garbage",
        [
            "",
            "   ",
            "not a url",
            # the CheckRun page form: the number is a check-run id, NOT a run id
            f"https://github.com/{REPO}/runs/109646322702",
            # scheme, host and host-suffix spoofs
            f"http://github.com/{REPO}/actions/runs/{RUN_G}/job/1",
            f"https://evil.example/{REPO}/actions/runs/{RUN_G}/job/1",
            f"https://github.com.evil.example/{REPO}/actions/runs/{RUN_G}/job/1",
            f"https://api.github.com/{REPO}/actions/runs/{RUN_G}/job/1",
            # malformed run / job ids
            f"https://github.com/{REPO}/actions/runs/abc/job/1",
            f"https://github.com/{REPO}/actions/runs/0{RUN_G}/job/1",
            f"https://github.com/{REPO}/actions/runs/{RUN_G}",
            f"https://github.com/{REPO}/actions/runs/{RUN_G}/job/",
            f"https://github.com/{REPO}/actions/runs/{RUN_G}/job/1/extra",
            f"https://github.com/{REPO}/actions/runs/{RUN_G}/job/1?x=1",
            # path games: the slug must be exactly OWNER/REPO
            f"https://github.com/acme/pub/extra/actions/runs/{RUN_G}/job/1",
            f"https://github.com/acme/actions/runs/{RUN_G}/job/1",
            f"https://github.com/acme/../pub/actions/runs/{RUN_G}/job/1",
        ],
    )
    @pytest.mark.parametrize("side", ["failure", "success"])
    def test_garbage_details_url_stays_red(self, garbage, side):
        fail = _run(
            "FAILURE",
            RUN_A,
            completed_at=FAIL_AT,
            details_url=garbage if side == "failure" else None,
        )
        ok = _run(
            "SUCCESS",
            RUN_B,
            completed_at=PASS_AT,
            details_url=garbage if side == "success" else None,
        )
        assert _drop(fail, ok) == [fail, ok]

    @pytest.mark.parametrize("side", ["failure", "success"])
    def test_garbage_cells_control_valid_form_supersedes(self, side):
        """GUARD-THE-GUARD for the garbage cells: the SAME run id (RUN_G) in the valid
        form must supersede, so each garbage cell fails on its shape alone, not on a
        run-id collision."""
        valid = f"https://github.com/{REPO}/actions/runs/{RUN_G}/job/1"
        fail = _run(
            "FAILURE", RUN_A, completed_at=FAIL_AT, details_url=valid if side == "failure" else None
        )
        ok = _run(
            "SUCCESS", RUN_B, completed_at=PASS_AT, details_url=valid if side == "success" else None
        )
        assert _drop(fail, ok) == [ok]

    def test_overlong_run_id_stays_red(self):
        """Ids are capped at 19 digits so int() is exact and bounded; a longer one is
        not a shape GitHub emits and is not parsed."""
        fail = _run("FAILURE", RUN_A, completed_at=FAIL_AT)
        ok = _run(
            "SUCCESS",
            completed_at=PASS_AT,
            details_url=f"https://github.com/{REPO}/actions/runs/{'9' * 20}/job/1",
        )
        assert _drop(fail, ok) == [fail, ok]

    def test_rollup_dedupe_consequence_newest_run_wins(self):
        """DOCUMENTED CONSEQUENCE, not a guarantee. The live rollup keeps only each
        run's latest attempt. Runs A < B both fail; B is re-attempted and passes, so the
        rollup shows A:FAILURE and B:SUCCESS. B is a newer run, so A's failure is
        cleared: re-attempting the newest run discharges older runs' failures. The
        pre-#2607 gate kept this red until A was re-run too. Pinned so a change to it
        is a visible decision."""
        fail_a = _run("FAILURE", RUN_A, completed_at=FAIL_AT)
        ok_b_latest_attempt = _run("SUCCESS", RUN_B, completed_at=PASS_AT)
        assert _drop(fail_a, ok_b_latest_attempt) == [ok_b_latest_attempt]

    def test_non_string_details_url_stays_red(self):
        fail = _run("FAILURE", RUN_A, completed_at=FAIL_AT)
        ok = _run("SUCCESS", completed_at=PASS_AT)
        ok["detailsUrl"] = 12345
        assert _drop(fail, ok) == [fail, ok]

    @pytest.mark.parametrize("side", ["failure", "success"])
    def test_foreign_repo_details_url_stays_red(self, side):
        fail = _run(
            "FAILURE",
            RUN_A,
            completed_at=FAIL_AT,
            slug="other/fork" if side == "failure" else REPO,
        )
        ok = _run(
            "SUCCESS",
            RUN_B,
            completed_at=PASS_AT,
            slug="other/fork" if side == "success" else REPO,
        )
        assert _drop(fail, ok) == [fail, ok]

    def test_slug_match_is_case_insensitive(self):
        """CONTROL for provenance: GitHub slugs are case-insensitive, so a URL that
        spells this repo in another case is still this repo."""
        fail = _run("FAILURE", RUN_A, completed_at=FAIL_AT)
        ok = _run("SUCCESS", RUN_B, completed_at=PASS_AT, slug="ACME/Pub")
        assert _drop(fail, ok) == [ok]

    def test_unresolvable_own_repo_stays_red(self, monkeypatch):
        """No repo identity means no provenance: a failure cannot be superseded."""
        monkeypatch.setenv("_TEST_GH_DERIVED_REPO", "")
        fail = _run("FAILURE", RUN_A, completed_at=FAIL_AT)
        ok = _run("SUCCESS", RUN_B, completed_at=PASS_AT)
        assert _drop(fail, ok, repo=None) == [fail, ok]

    def test_no_repo_lookup_when_nothing_could_qualify(self, monkeypatch):
        """The repo identity is resolved LAZILY: a payload with no qualifying
        failure/success pair costs no `gh repo view` on the merge path."""
        calls = []
        monkeypatch.setattr(_mod, "_derive_repo_from_cwd", lambda cwd: calls.append(cwd))
        _drop(
            _run("CANCELLED", RUN_A, completed_at=FAIL_AT),
            _run("SUCCESS", RUN_B, completed_at=PASS_AT),
            _run("FAILURE", RUN_A, completed_at=PASS_AT, name="lint"),  # no later success
            _run("FAILURE", completed_at=FAIL_AT, name="test"),  # no run id
            _run("SUCCESS", completed_at=PASS_AT, name="test"),
            repo=None,
        )
        assert calls == []
        # CONTROL: a pair that could qualify does trigger exactly one lookup.
        _drop(
            _run("FAILURE", RUN_A, completed_at=FAIL_AT),
            _run("FAILURE", RUN_A, completed_at=FAIL_AT, name="lint"),
            _run("SUCCESS", RUN_B, completed_at=PASS_AT),
            _run("SUCCESS", RUN_B, completed_at=PASS_AT, name="lint"),
            repo=None,
        )
        assert len(calls) == 1

    def test_repo_none_derives_from_cwd(self, monkeypatch):
        """CONTROL for the above: when the derived repo resolves, provenance holds."""
        monkeypatch.setenv("_TEST_GH_DERIVED_REPO", REPO)
        fail = _run("FAILURE", RUN_A, completed_at=FAIL_AT)
        ok = _run("SUCCESS", RUN_B, completed_at=PASS_AT)
        assert _drop(fail, ok, repo=None) == [ok]

    @pytest.mark.parametrize("conclusion", ["ACTION_REQUIRED", "STARTUP_FAILURE", "STALE"])
    def test_other_red_conclusions_are_never_superseded(self, conclusion):
        """Scope lock: only FAILURE and TIMED_OUT gained cross-run supersession."""
        red = _run(conclusion, RUN_A, completed_at=FAIL_AT)
        ok = _run("SUCCESS", RUN_B, completed_at=PASS_AT)
        assert _drop(red, ok) == [red, ok]

    def test_scope_lock_enumerates_every_non_droppable_red_conclusion(self):
        """DERIVED-SET GUARD: the parametrize above must cover the WHOLE remainder of
        _CI_RED_CONCLUSIONS, so a conclusion added to the constant cannot ship
        untested."""
        marks = TestFailureSupersessionPrimitive.test_other_red_conclusions_are_never_superseded
        parametrized = [m for m in marks.pytestmark if m.name == "parametrize"]
        assert len(parametrized) == 1
        covered = set(parametrized[0].args[1])
        assert covered == (
            set(_mod._CI_RED_CONCLUSIONS)
            - set(_mod._CI_CANCEL_CONCLUSIONS)
            - set(_mod._CI_CROSS_RUN_SUPERSEDABLE_CONCLUSIONS)
        )
        assert set(_mod._CI_CROSS_RUN_SUPERSEDABLE_CONCLUSIONS) == {"FAILURE", "TIMED_OUT"}

    def test_cancel_rule_is_unchanged_by_run_ids(self):
        """A cancel needs no run id and no different run: concurrency cancels are
        cross-run by nature, and the cancel rule predates this change untouched."""
        cancel = _run("CANCELLED", RUN_A, completed_at=FAIL_AT)
        ok = _run("SUCCESS", RUN_A, completed_at=PASS_AT)
        assert _drop(cancel, ok) == [ok]
        bare_cancel = _run("CANCELLED", completed_at=FAIL_AT)
        bare_ok = _run("SUCCESS", completed_at=PASS_AT)
        assert _drop(bare_cancel, bare_ok) == [bare_ok]

    def test_input_is_not_mutated(self):
        entries = [
            _run("FAILURE", RUN_A, completed_at=FAIL_AT),
            _run("SUCCESS", RUN_B, completed_at=PASS_AT),
        ]
        before = json.dumps(entries)
        _mod._drop_superseded_cancels(entries, repo=REPO)
        assert json.dumps(entries) == before


# ── Both consumers, one payload ─────────────────────────────────────────────────


class TestBothConsumersAgreeOnFailures:
    def test_cross_run_supersession_is_green_to_both(self, monkeypatch):
        assert _both(
            monkeypatch,
            _run("FAILURE", RUN_A, completed_at=FAIL_AT),
            _run("SUCCESS", RUN_B, completed_at=PASS_AT),
        ) == ("green", True)

    def test_timed_out_cross_run_is_green_to_both(self, monkeypatch):
        assert _both(
            monkeypatch,
            _run("TIMED_OUT", RUN_A, completed_at=FAIL_AT),
            _run("SUCCESS", RUN_B, completed_at=PASS_AT),
        ) == ("green", True)

    def test_same_run_reattempt_is_red_to_both(self, monkeypatch):
        assert _both(
            monkeypatch,
            _run("FAILURE", RUN_A, completed_at=FAIL_AT),
            _run("SUCCESS", RUN_A, completed_at=PASS_AT),
        ) == ("red", False)

    def test_cross_cover_via_reattempts_is_red_to_both(self, monkeypatch):
        assert _both(
            monkeypatch,
            _run("FAILURE", RUN_A, completed_at="2026-09-29T22:00:00Z"),
            _run("FAILURE", RUN_B, completed_at="2026-09-29T22:10:00Z"),
            _run("SUCCESS", RUN_A, completed_at="2026-09-29T22:20:00Z"),
            _run("SUCCESS", RUN_B, completed_at="2026-09-29T22:30:00Z"),
        ) == ("red", False)

    def test_tie_is_red_to_both(self, monkeypatch):
        assert _both(
            monkeypatch,
            _run("FAILURE", RUN_A, completed_at=PASS_AT),
            _run("SUCCESS", RUN_B, completed_at=PASS_AT),
        ) == ("red", False)

    def test_foreign_repo_is_red_to_both(self, monkeypatch):
        assert _both(
            monkeypatch,
            _run("FAILURE", RUN_A, completed_at=FAIL_AT),
            _run("SUCCESS", RUN_B, completed_at=PASS_AT, slug="other/fork"),
        ) == ("red", False)

    def test_different_workflow_is_red_to_both(self, monkeypatch):
        assert _both(
            monkeypatch,
            _run("FAILURE", RUN_A, completed_at=FAIL_AT),
            _run("SUCCESS", RUN_B, completed_at=PASS_AT, workflow="Decoy"),
        ) == ("red", False)


# ── Characterization: the real PR #2484 shape ───────────────────────────────────


class TestPr2484Characterization:
    """ACCEPTANCE BAR. PR #2484's head carried three `pull_request` runs of CI.
    lint and test FAILED in runs 36638934761 and 36647810311 (a broken base), then
    PASSED in 36651350733 after the base was fixed. Every other CI job passed in all
    three. Before #2607 the gate read `ci: red (lint, test)`. Run ids and completedAt
    values are the real ones; the repo slug is synthetic."""

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
        return [_run(c, run, completed_at=at, name=n, workflow="CI") for n, run, c, at in rows]

    def test_real_shape_reads_green(self, monkeypatch):
        state, green = _both(monkeypatch, *self._rollup(self.ROWS))
        assert state == "green"
        assert green is True

    def test_real_shape_reads_red_if_the_success_were_a_reattempt(self, monkeypatch):
        """CONTROL: the same rows, but the passing lint/test attempts carry run R2's
        id (a `gh run rerun` of R2). The newer-run rule must hold on the real shape,
        not only on a two-row fixture."""
        rows = [
            (n, self.R2 if (run == self.R3 and n in ("lint", "test")) else run, c, at)
            for n, run, c, at in self.ROWS
        ]
        state, _ = _both(monkeypatch, *self._rollup(rows))
        assert state == "red"
