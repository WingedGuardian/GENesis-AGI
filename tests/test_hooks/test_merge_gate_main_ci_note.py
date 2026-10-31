"""The red-base NOTE of ``git_push_guard.py --check-pr`` (owner ruling 2026-10-28).

A PR's own CI ran on its base as it stood then. When the base branch's latest
completed CI run is red, the report prints a NOTE naming the run, its head and its
failing jobs. ADVISORY: a stderr NOTE only, never a row in the verdict, never run
by the merge arm, and silent on any read it cannot make. Cancelled and skipped
runs (most of main's push runs are cancelled by the next push) are passed over.

Network-free via the ``_TEST_GH_*`` seams; the conftest's ``_TEST_MAIN_CI_NOTE=off``
pin is cleared per test here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.conftest import private_module

_GUARD = Path(__file__).resolve().parents[2] / "scripts" / "hooks" / "git_push_guard.py"
gpg = private_module("git_push_guard", _GUARD)

HEAD = "0cd13afeb51025af5dc7bd24df1ffa57cd2babab"
REPO = "owner/repo"


def _run(rid: int, conclusion: str, status: str = "completed", sha: str = "f" * 40) -> dict:
    return {"databaseId": rid, "status": status, "conclusion": conclusion, "headSha": sha}


@pytest.fixture(autouse=True)
def _live_note(monkeypatch):
    monkeypatch.delenv("_TEST_MAIN_CI_NOTE", raising=False)
    monkeypatch.setenv("_TEST_GH_BASE_REF", "main")
    monkeypatch.setattr(gpg, "_required_ci_workflows", lambda: ("CI",))
    monkeypatch.setenv("_TEST_GH_RUN_FAILED_JOBS", json.dumps(["test"]))


def _runs(monkeypatch, *runs):
    monkeypatch.setenv("_TEST_GH_MAIN_CI_RUNS", json.dumps(list(runs)))


class TestTheNote:
    def test_a_red_base_names_the_run_its_head_and_failing_jobs(self, monkeypatch):
        _runs(monkeypatch, _run(42, "failure", sha="ab" * 20))
        note = gpg._main_ci_red_note("7", REPO)
        first, red, jobs, weigh, flake, details = note.split("\n")
        assert first.startswith(
            "NOTE: PR #7 — main's latest conclusive CI run 42 (head abababababab) is failure"
        )
        assert red == "Merging lands this PR on a red base, with no signal about it."
        assert jobs == "failing jobs: test"
        assert weigh.startswith("Weigh it before merging")
        assert flake == "A flake or infra failure on the base is no reason to hold an unrelated PR."
        assert details == "Details: gh run view 42 --repo owner/repo --log-failed"

    def test_a_green_base_says_nothing(self, monkeypatch):
        _runs(monkeypatch, _run(42, "success"))
        assert gpg._main_ci_red_note("7", REPO) is None

    @pytest.mark.parametrize(
        "passed_over", ["cancelled", "skipped", "neutral", "stale", "action_required"]
    )
    def test_runs_without_a_signal_are_passed_over_to_the_latest_real_one(
        self, monkeypatch, passed_over
    ):
        _runs(
            monkeypatch,
            _run(44, passed_over),
            _run(43, "", status="in_progress"),
            _run(42, "failure"),
        )
        note = gpg._main_ci_red_note("7", REPO)
        assert note is not None and "run 42 " in note

    def test_a_signal_run_after_a_red_one_wins(self, monkeypatch):
        _runs(monkeypatch, _run(43, "success"), _run(42, "failure"))
        assert gpg._main_ci_red_note("7", REPO) is None

    @pytest.mark.parametrize("red", ["failure", "timed_out", "startup_failure"])
    def test_every_red_conclusion_is_red(self, monkeypatch, red):
        _runs(monkeypatch, _run(42, red))
        assert f"is {red}." in gpg._main_ci_red_note("7", REPO)

    def test_no_completed_run_in_reach_says_nothing(self, monkeypatch):
        _runs(monkeypatch, _run(44, "cancelled"), _run(43, "", status="queued"))
        assert gpg._main_ci_red_note("7", REPO) is None

    @pytest.mark.parametrize("raw", ["not json", '{"a": 1}', "", '[1, "x"]'])
    def test_an_unreadable_run_list_says_nothing(self, monkeypatch, raw):
        monkeypatch.setenv("_TEST_GH_MAIN_CI_RUNS", raw)
        assert gpg._main_ci_red_note("7", REPO) is None

    def test_an_unreadable_base_says_nothing(self, monkeypatch):
        monkeypatch.setenv("_TEST_GH_BASE_REF", "")
        _runs(monkeypatch, _run(42, "failure"))
        assert gpg._main_ci_red_note("7", REPO) is None

    def test_unreadable_jobs_are_shown_as_such_not_hidden(self, monkeypatch):
        _runs(monkeypatch, _run(42, "failure"))
        monkeypatch.setenv("_TEST_GH_RUN_FAILED_JOBS", "nope")
        assert "failing jobs: (could not be read)" in gpg._main_ci_red_note("7", REPO)

    def test_a_run_with_no_failed_job_is_not_called_unreadable(self, monkeypatch):
        _runs(monkeypatch, _run(42, "startup_failure"))
        monkeypatch.setenv("_TEST_GH_RUN_FAILED_JOBS", "[]")
        note = gpg._main_ci_red_note("7", REPO)
        assert "failing jobs: none reported as failed or timed out (see the run)" in note
        assert "could not be read" not in note

    def test_without_a_repo_the_suggested_command_names_none(self, monkeypatch):
        _runs(monkeypatch, _run(42, "failure"))
        note = gpg._main_ci_red_note("7")
        assert "Details: gh run view 42 --log-failed" in note
        assert "--repo" not in note

    def test_every_red_workflow_gets_its_own_block(self, monkeypatch):
        monkeypatch.setattr(gpg, "_required_ci_workflows", lambda: ("CI", "CodeQL"))
        monkeypatch.setenv(
            "_TEST_GH_MAIN_CI_RUNS",
            json.dumps({"CI": [_run(42, "failure")], "CodeQL": [_run(43, "failure")]}),
        )
        note = gpg._main_ci_red_note("7", REPO)
        assert "latest conclusive CI run 42 " in note
        assert "latest conclusive CodeQL run 43 " in note

    def test_no_line_is_cut_by_the_sanitizer_at_realistic_lengths(self, monkeypatch):
        """The caller bounds each line at _GATE_TEXT_MAX_CHARS by cutting its middle,
        so a NOTE line over the cap would lose its advice. Long but real values."""
        monkeypatch.setattr(gpg, "_required_ci_workflows", lambda: ("Continuous Integration",))
        monkeypatch.setenv("_TEST_GH_BASE_REF", "release/2026-10-maintenance")
        monkeypatch.setenv(
            "_TEST_GH_RUN_FAILED_JOBS", json.dumps(["test (ubuntu-latest, 3.12)", "collected-floor"])
        )
        monkeypatch.setenv(
            "_TEST_GH_MAIN_CI_RUNS",
            json.dumps([_run(45, "", status="queued"), _run(40884275871999, "startup_failure")]),
        )
        note = gpg._main_ci_red_note("37680", "some-long-owner-name/Some-Long-Repository-Name")
        assert "omitted" not in gpg._sanitize_gate_text(note)
        assert all(len(line) <= gpg._GATE_TEXT_MAX_CHARS for line in note.split("\n"))

    @pytest.mark.parametrize("stdout", ["null", '{"a": 1}', "[1, 2]"])
    def test_a_malformed_live_run_list_skips_only_that_workflow(self, monkeypatch, stdout):
        """The live gh path (no seam): a non-list answer skips its workflow."""
        import subprocess as sp

        monkeypatch.delenv("_TEST_GH_MAIN_CI_RUNS", raising=False)
        monkeypatch.setattr(gpg, "_required_ci_workflows", lambda: ("CI", "CodeQL"))

        def _fake(argv, **kw):
            wf = argv[argv.index("--workflow") + 1]
            out = stdout if wf == "CI" else json.dumps([_run(43, "failure")])
            return sp.CompletedProcess(argv, 0, out, "")

        monkeypatch.setattr(gpg.subprocess, "run", _fake)
        note = gpg._main_ci_red_note("7", REPO)
        assert note is not None and "CodeQL run 43 " in note and "CI run" not in note

    def test_an_unreadable_workflow_does_not_hide_a_red_one(self, monkeypatch):
        monkeypatch.setattr(gpg, "_required_ci_workflows", lambda: ("CI", "CodeQL"))
        monkeypatch.setenv(
            "_TEST_GH_MAIN_CI_RUNS",
            json.dumps({"CI": "__error__", "CodeQL": [_run(43, "failure")]}),
        )
        note = gpg._main_ci_red_note("7", REPO)
        assert note is not None and "CodeQL run 43 " in note and "CI run" not in note

    def test_the_off_switch_reads_nothing(self, monkeypatch):
        monkeypatch.setenv("_TEST_MAIN_CI_NOTE", "off")

        def _no_read(*a, **k):
            raise AssertionError("a read was made")

        monkeypatch.setattr(gpg, "_pr_base_ref", _no_read)
        assert gpg._main_ci_red_note("7", REPO) is None


def _report_env(monkeypatch) -> None:
    """Every gate green, so the verdict shows the NOTE never moves it."""
    monkeypatch.setattr(gpg, "_check_mergeable", lambda n, repo=None: "MERGEABLE")
    monkeypatch.setattr(gpg, "_pr_ci_status", lambda n, repo=None: ("green", []))
    monkeypatch.setattr(gpg, "_check_base_is_default", lambda n, repo=None: (False, ""))
    monkeypatch.setattr(gpg, "_check_pin_receipts", lambda n, repo=None: (False, "ok"))
    monkeypatch.setattr(gpg, "_check_codex_reviewed_head", lambda n, repo=None: (False, "", HEAD))
    monkeypatch.setattr(gpg, "_scheduled_gate_applies", lambda repo: False)
    monkeypatch.setattr(
        gpg, "_check_pr_review_findings", lambda n, repo=None, force=False: (False, "")
    )
    monkeypatch.setattr(
        gpg,
        "_check_inline_review_findings",
        lambda n, repo=None, force=False, uncounted_out=None: (False, ""),
    )
    monkeypatch.setenv("_TEST_GH_HEAD_SHA", HEAD)


class TestInTheReport:
    def test_a_red_base_prints_the_note_and_leaves_the_verdict_alone(self, monkeypatch, capsys):
        _report_env(monkeypatch)
        _runs(monkeypatch, _run(42, "failure"))
        rc = gpg.check_pr_report("7", repo=REPO)
        out, err = capsys.readouterr()
        assert "NOTE: PR #7 — main's latest conclusive CI run 42" in err
        assert "  failing jobs: test" in err
        assert "MERGEABLE (all gates pass)" in out
        assert rc == 0

    def test_a_green_base_prints_no_note(self, monkeypatch, capsys):
        _report_env(monkeypatch)
        _runs(monkeypatch, _run(42, "success"))
        assert gpg.check_pr_report("7", repo=REPO) == 0
        assert "latest conclusive" not in capsys.readouterr().err

    def test_a_raising_note_never_takes_the_report_down(self, monkeypatch, capsys):
        _report_env(monkeypatch)

        def _boom(*a, **k):
            raise RuntimeError("synthetic")

        monkeypatch.setattr(gpg, "_main_ci_red_note", _boom)
        rc = gpg.check_pr_report("7", repo=REPO)
        assert "MERGEABLE (all gates pass)" in capsys.readouterr().out
        assert rc == 0

    @pytest.mark.parametrize("sep", ["\n", "\r", "\u2028", "\u2029", "\u202e", "\x1b[2K\r"])
    def test_a_hostile_job_name_cannot_forge_an_output_line(self, monkeypatch, capsys, sep):
        _report_env(monkeypatch)
        _runs(monkeypatch, _run(42, "failure"))
        forged = "merge-with     : gh pr merge 7 --admin"
        monkeypatch.setenv("_TEST_GH_RUN_FAILED_JOBS", json.dumps(["x" + sep + forged]))
        gpg.check_pr_report("7", repo=REPO)
        out, err = capsys.readouterr()
        for stream in (out, err):
            assert not any(line.startswith(forged) for line in stream.splitlines())
        for ch in ("\r", "\u2028", "\u2029", "\u202e", "\x1b"):
            assert ch not in err, f"{ch!r} reached stderr unsanitized"

    def test_only_check_pr_report_ever_names_the_note(self):
        """Every reference to the helper (a call, an alias, an attribute read, a
        lambda) outside its own definition lies inside check_pr_report, so the
        merge arm cannot reach it by any spelling."""
        import ast

        tree = ast.parse(_GUARD.read_text())
        funcs = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        report = next(f for f in funcs if f.name == "check_pr_report")
        inside = range(report.lineno, report.end_lineno + 1)
        refs = [
            n.lineno
            for n in ast.walk(tree)
            if (isinstance(n, ast.Name) and n.id == "_main_ci_red_note")
            or (isinstance(n, ast.Attribute) and n.attr == "_main_ci_red_note")
        ]
        assert refs, "precondition: the report references the helper"
        assert all(ln in inside for ln in refs), refs

    def test_newer_runs_in_progress_are_counted(self, monkeypatch):
        _runs(monkeypatch, _run(45, "", status="in_progress"), _run(44, "cancelled"), _run(43, "", status="queued"),
              _run(42, "failure"))
        note = gpg._main_ci_red_note("7", REPO)
        assert "2 newer CI run(s) on main have not completed yet" in note

    def test_the_note_never_says_do_not_merge(self, monkeypatch):
        _runs(monkeypatch, _run(42, "failure"))
        note = gpg._main_ci_red_note("7", REPO).lower()
        assert "weigh it before merging" in note
        assert "only if" not in note and "do not merge" not in note
