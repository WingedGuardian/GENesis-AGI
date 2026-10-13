"""The newest workflow run per workflow decides CI (issue #2607).

WHY. ``statusCheckRollup`` keeps every workflow run on the head commit. After a
base-branch fix, a fresh ``pull_request`` run on the SAME head passes, but the earlier
run's FAILURE is still in the rollup, so the gate read ``ci: red`` on a PR whose latest
run is green.

THE RULE, in the one shared primitive ``_newest_run_per_workflow``: the workflow RUN id
is parsed strictly from each Actions CheckRun's ``detailsUrl``; per ``workflowName`` the
highest run id is the newest run, and every entry of that workflow from an OLDER run is
dropped. All entries of the newest run are kept exactly as they are. Anything whose run
cannot be established (no identity, an unparseable or foreign URL, an unresolvable repo)
is always kept and never causes a drop.

WHY THE RUN AND NOT THE JOB. A per-job "latest result" rule synthesizes green from two
failed runs (Codex P1 on this change): older run test=FAILURE + late lint=SUCCESS, newer
run lint=FAILURE + test=SUCCESS. The interleaved test below must read red.

NOT A RE-RUN GUARD. GitHub's rollup lists only a re-run's LATEST attempt, so
re-run-until-green is not blocked by this gate, before or after this change (#2624).

Both consumers (``_pr_ci_status`` and ``_mechanical_scan_is_green``) get the rule
through the helper; the agreement tests below lock that. Network-free:
``_TEST_GH_CI_ROLLUP`` / ``_TEST_GH_ROLLUP_WITH_HEAD`` / ``_TEST_GH_DERIVED_REPO``.
"""

from __future__ import annotations

import itertools
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
RA, RB, RC = 36638934761, 36647810311, 36651350733
_JOBS = itertools.count(109600000001)


def _url(run_id, *, slug=REPO):
    return f"https://github.com/{slug}/actions/runs/{run_id}/job/{next(_JOBS)}"


def _run(
    conclusion,
    run_id=None,
    *,
    name="leak-detector",
    workflow="CI",
    status="COMPLETED",
    completed_at="2026-09-30T00:00:00Z",
    slug=REPO,
    details_url=None,
):
    entry = {
        "__typename": "CheckRun",
        "name": name,
        "workflowName": workflow,
        "status": status,
        "conclusion": conclusion,
        "completedAt": completed_at,
    }
    if details_url is not None:
        entry["detailsUrl"] = details_url
    elif run_id is not None:
        entry["detailsUrl"] = _url(run_id, slug=slug)
    return entry


def _keep(*entries, repo=REPO):
    return _mod._newest_run_per_workflow(list(entries), repo=repo)


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


# ── The acceptance bars ──────────────────────────────────────────────────────


class TestAcceptance:
    def test_codex_interleaved_runs_read_red(self, monkeypatch):
        """Codex P1: neither run passed, so the head must not read green. Older run A:
        test=FAILURE then a late lint=SUCCESS; newer run B: lint=FAILURE then
        test=SUCCESS. A per-job latest-result rule kept A.lint and B.test."""
        rollup = [
            _run("FAILURE", RA, name="test", completed_at="2026-09-30T00:00:01Z"),
            _run("SUCCESS", RA, name="lint", completed_at="2026-09-30T00:00:09Z"),
            _run("FAILURE", RB, name="lint", completed_at="2026-09-30T00:00:05Z"),
            _run("SUCCESS", RB, name="test", completed_at="2026-09-30T00:00:07Z"),
        ]
        assert _both(monkeypatch, *rollup)[0] == "red"
        assert [e["name"] for e in _keep(*rollup)] == ["lint", "test"]
        assert all(e["detailsUrl"].split("/")[-3] == str(RB) for e in _keep(*rollup))

    def test_codex_interleaved_runs_scanner_side(self, monkeypatch):
        """The same shape on the leaks relief path: the scanner failed in the newest
        run, so relief is refused even though an older run's scanner passed later."""
        rollup = [
            _run("SUCCESS", RA, completed_at="2026-09-30T00:00:09Z"),
            _run("FAILURE", RB, completed_at="2026-09-30T00:00:05Z"),
        ]
        assert _both(monkeypatch, *rollup) == ("red", False)


class TestPr2484Characterization:
    """ACCEPTANCE BAR. PR #2484's head carried three `pull_request` runs of CI.
    lint and test FAILED in runs 36638934761 and 36647810311 (a broken base), then
    PASSED in 36651350733 after the base was fixed. Every other CI job passed in all
    three. Before #2607 the gate read `ci: red (lint, test)`. Run ids and completedAt
    values are the real ones; the repo slug is synthetic."""

    ROWS = [
        ("lint", RA, "FAILURE", "2026-09-29T22:20:11Z"),
        ("lint", RB, "FAILURE", "2026-09-29T23:56:53Z"),
        ("lint", RC, "SUCCESS", "2026-09-30T00:39:24Z"),
        ("test", RA, "FAILURE", "2026-09-29T23:01:31Z"),
        ("test", RB, "FAILURE", "2026-09-30T00:29:21Z"),
        ("test", RC, "SUCCESS", "2026-09-30T01:26:58Z"),
        ("leak-detector", RA, "SUCCESS", "2026-09-29T22:21:05Z"),
        ("leak-detector", RB, "SUCCESS", "2026-09-29T23:57:55Z"),
        ("leak-detector", RC, "SUCCESS", "2026-09-30T00:40:15Z"),
        ("migration-check", RA, "SUCCESS", "2026-09-29T22:20:15Z"),
        ("migration-check", RB, "SUCCESS", "2026-09-29T23:56:51Z"),
        ("migration-check", RC, "SUCCESS", "2026-09-30T00:39:13Z"),
    ]

    def _rollup(self, rows):
        return [_run(c, run, name=n, completed_at=at) for n, run, c, at in rows]

    def test_real_shape_reads_green(self, monkeypatch):
        assert _both(monkeypatch, *self._rollup(self.ROWS)) == ("green", True)

    def test_real_shape_reads_red_if_the_last_run_had_failed(self, monkeypatch):
        """CONTROL: flip run 3's `test` to FAILURE — the newest run decides, so red."""
        rows = [
            (n, run, "FAILURE" if (n == "test" and run == RC) else c, at)
            for n, run, c, at in self.ROWS
        ]
        assert _both(monkeypatch, *self._rollup(rows))[0] == "red"


# ── The newest run decides, as a whole ───────────────────────────────────────


class TestNewestRunDecides:
    def test_newer_passing_run_clears_an_older_failure(self, monkeypatch):
        assert _both(monkeypatch, _run("FAILURE", RA), _run("SUCCESS", RB)) == ("green", True)

    def test_newer_failed_run_is_red_even_if_the_older_was_green(self, monkeypatch):
        assert _both(monkeypatch, _run("SUCCESS", RA), _run("FAILURE", RB)) == ("red", False)

    def test_newer_run_in_progress_is_pending_even_if_the_older_was_green(self, monkeypatch):
        state, green = _both(
            monkeypatch,
            _run("SUCCESS", RA),
            _run("SUCCESS", RA, name="lint"),
            _run(None, RB, status="IN_PROGRESS"),
            _run("SUCCESS", RB, name="lint"),
        )
        assert state == "pending"
        assert green is False

    def test_newer_cancelled_run_is_red_even_if_the_older_was_green(self, monkeypatch):
        assert _both(monkeypatch, _run("SUCCESS", RA), _run("CANCELLED", RB)) == ("red", False)

    def test_newer_run_that_skipped_a_job_drops_its_older_failure(self, monkeypatch):
        """DOCUMENTED CONSEQUENCE of 'the newest run decides': the newest run skipped
        `test`, so the older run's `test` FAILURE is dropped with the rest of that run.
        This is how GitHub itself presents a PR (latest run per workflow)."""
        rollup = [
            _run("FAILURE", RA, name="test"),
            _run("SUCCESS", RA, name="lint"),
            _run("SKIPPED", RB, name="test"),
            _run("SUCCESS", RB, name="lint"),
        ]
        kept = _keep(*rollup)
        assert rollup[0] not in kept and rollup[2] in kept
        monkeypatch.setenv("_TEST_GH_CI_ROLLUP", json.dumps(rollup))
        monkeypatch.setenv("_TEST_REQUIRED_CI_WORKFLOWS", "CI")
        assert _mod._pr_ci_status("1", repo=REPO)[0] == "green"

    def test_whole_newest_run_is_kept_including_duplicates(self):
        """Within the newest run nothing supersedes anything: a job listed twice (the
        rollup should not do this) keeps both entries, so a red one still reads red."""
        a = _run("FAILURE", RB)
        b = _run("SUCCESS", RB)
        c = _run(None, RB, name="lint", status="QUEUED")
        d = _run("SKIPPED", RB, name="docs")
        assert _keep(_run("SUCCESS", RA), a, b, c, d) == [a, b, c, d]

    def test_a_job_absent_from_the_newest_run_is_dropped_with_its_run(self):
        """The unit is the RUN: an older run's job that the newest run did not publish
        is dropped with the rest of that older run, not kept as that job's own latest
        result. (A per-job reduction would keep the older `docs` entry.)"""
        old_docs = _run("FAILURE", RA, name="docs")
        old_test = _run("FAILURE", RA, name="test")
        new_test = _run("SUCCESS", RB, name="test")
        assert _keep(old_docs, old_test, new_test) == [new_test]

    def test_three_runs_only_the_newest_survives(self):
        a, b, c = _run("FAILURE", RA), _run("SUCCESS", RB), _run("FAILURE", RC)
        assert _keep(a, b, c) == [c]

    def test_input_order_does_not_matter(self):
        old, new = _run("FAILURE", RA), _run("SUCCESS", RB)
        assert _keep(new, old) == [new]

    def test_timestamps_are_not_consulted(self):
        """The newer RUN wins even when its entry reports an earlier completedAt."""
        old = _run("FAILURE", RA, completed_at="2026-09-30T02:00:00Z")
        new = _run("SUCCESS", RB, completed_at="2026-09-30T01:00:00Z")
        assert _keep(old, new) == [new]

    def test_two_workflows_are_judged_independently(self, monkeypatch):
        """CodeQL's newer run id must not drop CI's entries, and vice versa."""
        ci_fail = _run("FAILURE", RA, name="test")
        codeql_ok = _run("SUCCESS", RC, name="Analyze", workflow="CodeQL")
        assert _keep(ci_fail, codeql_ok) == [ci_fail, codeql_ok]
        assert _both(monkeypatch, ci_fail, codeql_ok)[0] == "red"
        ci_ok_new = _run("SUCCESS", RB, name="test")
        codeql_fail_old = _run("FAILURE", RA, name="Analyze", workflow="CodeQL")
        assert _keep(ci_fail, ci_ok_new, codeql_fail_old) == [ci_ok_new, codeql_fail_old]

    def test_different_workflow_with_a_newer_run_cannot_clear_ci(self, monkeypatch):
        assert _both(monkeypatch, _run("FAILURE", RA), _run("SUCCESS", RB, workflow="Decoy")) == (
            "red",
            False,
        )


# ── Fail closed: anything whose run cannot be established is kept ───────────


class TestFailClosed:
    @pytest.mark.parametrize("side", ["older", "newer"])
    def test_missing_details_url_is_kept(self, side):
        old = _run("FAILURE", None if side == "older" else RA)
        new = _run("SUCCESS", None if side == "newer" else RB)
        assert _keep(old, new) == [old, new]

    @pytest.mark.parametrize(
        "garbage",
        [
            "",
            "   ",
            "not a url",
            f"https://github.com/{REPO}/runs/109646322702",  # check-run page, not a run
            f"http://github.com/{REPO}/actions/runs/{RC}/job/1",
            f"https://evil.example/{REPO}/actions/runs/{RC}/job/1",
            f"https://github.com.evil.example/{REPO}/actions/runs/{RC}/job/1",
            f"https://api.github.com/{REPO}/actions/runs/{RC}/job/1",
            f"https://github.com/{REPO}/actions/runs/abc/job/1",
            f"https://github.com/{REPO}/actions/runs/0{RC}/job/1",
            f"https://github.com/{REPO}/actions/runs/{'9' * 20}/job/1",
            f"https://github.com/{REPO}/actions/runs/{RC}",
            f"https://github.com/{REPO}/actions/runs/{RC}/job/",
            f"https://github.com/{REPO}/actions/runs/{RC}/job/1/extra",
            f"https://github.com/{REPO}/actions/runs/{RC}/job/1?x=1",
            f"https://github.com/acme/pub/extra/actions/runs/{RC}/job/1",
            f"https://github.com/acme/actions/runs/{RC}/job/1",
        ],
    )
    def test_garbage_url_on_the_newer_entry_never_drops_the_older(self, garbage):
        old = _run("FAILURE", RA)
        new = _run("SUCCESS", details_url=garbage)
        assert _keep(old, new) == [old, new]

    def test_garbage_cells_control_valid_form_drops(self):
        """GUARD-THE-GUARD: the same run id in the valid form DOES supersede, so each
        garbage cell fails on its shape alone."""
        old = _run("FAILURE", RA)
        new = _run("SUCCESS", details_url=f"https://github.com/{REPO}/actions/runs/{RC}/job/1")
        assert _keep(old, new) == [new]

    def test_non_string_details_url_is_kept(self):
        old = _run("FAILURE", RA)
        new = _run("SUCCESS")
        new["detailsUrl"] = 12345
        assert _keep(old, new) == [old, new]

    @pytest.mark.parametrize("side", ["older", "newer"])
    def test_foreign_repo_url_is_kept_and_drops_nothing(self, side):
        old = _run("FAILURE", RA, slug="other/fork" if side == "older" else REPO)
        new = _run("SUCCESS", RB, slug="other/fork" if side == "newer" else REPO)
        assert _keep(old, new) == [old, new]

    def test_slug_match_is_case_insensitive(self):
        old = _run("FAILURE", RA)
        new = _run("SUCCESS", RB, slug="ACME/Pub")
        assert _keep(old, new) == [new]

    def test_unresolvable_repo_drops_nothing(self, monkeypatch):
        monkeypatch.setenv("_TEST_GH_DERIVED_REPO", "")
        old, new = _run("FAILURE", RA), _run("SUCCESS", RB)
        assert _keep(old, new, repo=None) == [old, new]

    def test_repo_none_derives_from_cwd(self, monkeypatch):
        monkeypatch.setenv("_TEST_GH_DERIVED_REPO", REPO)
        old, new = _run("FAILURE", RA), _run("SUCCESS", RB)
        assert _keep(old, new, repo=None) == [new]

    def test_no_repo_lookup_when_nothing_has_a_run_url(self, monkeypatch):
        calls = []
        monkeypatch.setattr(_mod, "_derive_repo_from_cwd", lambda cwd: calls.append(cwd))
        _keep(_run("FAILURE"), _run("SUCCESS"), {"context": "x", "state": "SUCCESS"}, repo=None)
        assert calls == []
        _keep(_run("FAILURE", RA), _run("SUCCESS", RB), repo=None)
        assert len(calls) == 1

    def test_no_identity_is_kept_and_takes_no_part(self):
        """A StatusContext or a non-Actions check (no workflowName) is never dropped,
        and its run-looking URL cannot make it the newest run of anything."""
        old = _run("FAILURE", RA)
        legacy = {"context": "leak-detector", "state": "SUCCESS", "detailsUrl": _url(RC)}
        no_wf = {
            "name": "leak-detector",
            "status": "COMPLETED",
            "conclusion": "SUCCESS",
            "detailsUrl": _url(RC),
        }
        assert _keep(old, legacy, no_wf) == [old, legacy, no_wf]

    def test_unparseable_entry_of_an_older_run_style_is_kept_beside_the_newest(self):
        """A workflow with a newest run still keeps its unparseable entries."""
        bare_fail = _run("FAILURE")
        old, new = _run("SUCCESS", RA), _run("SUCCESS", RB)
        assert _keep(bare_fail, old, new) == [bare_fail, new]

    def test_non_dict_entries_pass_through(self):
        payload = ["junk", None, _run("SUCCESS", RA)]
        assert _keep(*payload) == payload

    def test_input_is_not_mutated(self):
        entries = [_run("FAILURE", RA), _run("SUCCESS", RB)]
        before = json.dumps(entries)
        _mod._newest_run_per_workflow(entries, repo=REPO)
        assert json.dumps(entries) == before
