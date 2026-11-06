"""The weekly corpus run's one issue: opened once, updated only when findings change,
and never on a run that did not finish."""

from __future__ import annotations

import importlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

_REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO / "scripts" / "ci"))
gi = importlib.import_module("gate_corpus_issue")
mr = importlib.import_module("mutation_rungs")

URL = "https://example.test/run/1"


class FakeGh:
    """Answers `gh` from a small issue store, and records every write."""

    def __init__(self, issue=None, incomplete=False):
        self.issue = issue
        self.incomplete = incomplete
        self.writes = []

    def __call__(self, argv):
        if argv[0] == "api":
            items = [self.issue] if self.issue else []
            return json.dumps({"incomplete_results": self.incomplete, "items": items})
        if argv[:2] == ["issue", "view"]:
            return json.dumps(
                {
                    "body": self.issue.get("body", ""),
                    "comments": self.issue.get("comments", []),
                }
            )
        self.writes.append(argv)
        return ""


def _findings(tmp_path, findings, complete=True):
    path = tmp_path / "findings.json"
    path.write_text(json.dumps({"complete": complete, "findings": findings}), encoding="utf-8")
    return path


def _file(tmp_path, gh, findings=("x ESCAPED",), complete=True):
    return gi.file_findings(_findings(tmp_path, list(findings), complete), "o/r", URL, gh)


def test_no_findings_writes_nothing(tmp_path):
    gh = FakeGh()
    assert _file(tmp_path, gh, findings=()) == "nothing to report"
    assert gh.writes == []


def test_a_run_that_did_not_finish_writes_nothing(tmp_path):
    gh = FakeGh()
    assert "did not finish" in _file(tmp_path, gh, complete=False)
    assert gh.writes == []


def test_the_first_finding_opens_one_issue(tmp_path):
    gh = FakeGh()
    assert _file(tmp_path, gh) == "opened an issue"
    assert gh.writes[0][:2] == ["issue", "create"]
    assert gi.TITLE in gh.writes[0] and "--repo" in gh.writes[0]
    body = gh.writes[0][gh.writes[0].index("--body") + 1]
    assert "x ESCAPED" in body and URL in body


def test_a_similar_title_is_not_this_issue(tmp_path):
    gh = FakeGh({"number": 5, "title": gi.TITLE + " again"})
    assert _file(tmp_path, gh) == "opened an issue"


def test_the_search_only_finds_the_workflows_own_issue(tmp_path):
    seen = []

    def gh(argv):
        seen.append(argv)
        return json.dumps({"incomplete_results": False, "items": []})

    _file(tmp_path, gh)
    assert "author:app/github-actions" in seen[0][-1]


def _own(body):
    return {"author": {"login": "github-actions"}, "body": body}


def test_a_stranger_posting_the_marker_cannot_silence_a_finding(tmp_path):
    gh = FakeGh(
        {
            "number": 5,
            "title": gi.TITLE,
            "body": gi._body(["old ESCAPED"], URL),
            "comments": [{"author": {"login": "someone"}, "body": gi._body(["x ESCAPED"], URL)}],
        }
    )
    assert _file(tmp_path, gh) == "updated #5"


def test_new_findings_comment_on_the_open_issue(tmp_path):
    old = gi._body(["old ESCAPED"], URL)
    gh = FakeGh({"number": 5, "title": gi.TITLE, "body": old})
    assert _file(tmp_path, gh) == "updated #5"
    assert gh.writes[0][:3] == ["issue", "comment", "5"]


def test_the_same_findings_add_no_comment_whatever_the_run(tmp_path):
    same = gi._body(["x ESCAPED"], "https://example.test/run/0")
    gh = FakeGh({"number": 5, "title": gi.TITLE, "body": same})
    assert _file(tmp_path, gh) == "#5 already says this"
    assert gh.writes == []


def test_only_the_newest_findings_count(tmp_path):
    """Findings that return after changing are news, though the body still has them."""
    gh = FakeGh(
        {
            "number": 5,
            "title": gi.TITLE,
            "body": gi._body(["x ESCAPED"], URL),
            "comments": [_own(gi._body(["y ESCAPED"], URL))],
        }
    )
    assert _file(tmp_path, gh) == "updated #5"


def test_an_incomplete_search_refuses_rather_than_open_a_second_issue(tmp_path):
    gh = FakeGh(incomplete=True)
    with pytest.raises(RuntimeError, match="incomplete"):
        _file(tmp_path, gh)
    assert gh.writes == []


@pytest.mark.parametrize("bad", [None, "x", [1]], ids=["null", "string", "not-strings"])
def test_malformed_findings_are_refused(tmp_path, bad):
    path = tmp_path / "f.json"
    path.write_text(json.dumps({"complete": True, "findings": bad}), encoding="utf-8")
    with pytest.raises(ValueError, match="findings"):
        gi.file_findings(path, "o/r", URL, FakeGh())


def test_main_goes_red_when_gh_fails(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")

    def fails(argv):
        raise subprocess.CalledProcessError(1, argv)

    monkeypatch.setattr(gi, "_gh", fails)
    monkeypatch.setattr(gi.file_findings, "__defaults__", (fails,))
    assert gi.main([str(_findings(tmp_path, ["x ESCAPED"]))]) == 1


# ── The report's findings ────────────────────────────────────────────────────


def _report(*defects, outcomes, rungs=("a", "b")):
    owners = {}
    report = mr.Report(
        [mr._Defect(label, aim, tuple(rungs)) for label, aim in defects],
        owners,
        {r: ("t.py",) for r in rungs},
    )
    for key, outcome in outcomes.items():
        result = mr.Result(object(), outcome)
        owners[id(result.case)] = key
        report.add(result)
    return report


def _all(outcome):
    return {("d", "a"): outcome, ("d", "b"): outcome}


def test_an_escape_is_a_finding():
    findings = _report(("d", "a"), outcomes=_all("SURVIVED")).findings()
    assert findings == ["d (aim: a): ESCAPED"]


def test_a_catch_off_aim_is_a_finding():
    report = _report(("d", "a"), outcomes={("d", "a"): "SURVIVED", ("d", "b"): mr.BIT})
    assert report.findings() == ["d (aim: a): caught only by b"]


def test_a_catch_on_aim_is_no_finding():
    report = _report(("d", "a"), outcomes={("d", "a"): mr.BIT, ("d", "b"): "SURVIVED"})
    assert report.findings() == []


def test_an_untrusted_run_finds_nothing_about_that_defect():
    report = _report(("d", "a"), outcomes={("d", "a"): mr.ABORTED, ("d", "b"): "SURVIVED"})
    assert report.findings() == []


def test_a_cut_short_run_finds_nothing():
    report = _report(("d", "a"), outcomes={("d", "a"): "SURVIVED"})
    assert report.findings() == []
    assert report.complete() is False


def test_a_rung_that_caught_nothing_is_a_finding_from_two_defects():
    outcomes = {
        ("d", "a"): mr.BIT,
        ("d", "b"): "SURVIVED",
        ("e", "a"): mr.BIT,
        ("e", "b"): "SURVIVED",
    }
    assert _report(("d", "a"), ("e", "a"), outcomes=outcomes).findings() == [
        "rung `b`: caught nothing"
    ]


def test_the_findings_file_records_whether_the_run_finished(tmp_path):
    report = _report(("d", "a"), outcomes={("d", "a"): "SURVIVED"})
    path = tmp_path / "f.json"
    report.write_findings(path)
    assert json.loads(path.read_text()) == {"complete": False, "findings": []}


# ── The job ──────────────────────────────────────────────────────────────────


def _jobs():
    text = (_REPO / ".github" / "workflows" / "gate-corpus.yml").read_text(encoding="utf-8")
    return yaml.safe_load(text)["jobs"]


def test_only_the_issue_job_can_write_and_only_off_pull_requests():
    jobs = _jobs()
    assert "permissions" not in jobs["report"], "it runs the test suite: no write token"
    issue = jobs["file-issue"]
    assert issue["permissions"] == {"contents": "read", "issues": "write"}
    assert "pull_request" in issue["if"] and "refs/heads/main" in issue["if"]


def test_one_aborted_case_does_not_hide_the_others_findings():
    jobs = _jobs()
    issue = jobs["file-issue"]
    assert issue["needs"] == "report"
    assert "!cancelled()" in issue["if"]
    upload = next(s for s in jobs["report"]["steps"] if "upload-artifact" in s.get("uses", ""))
    assert "!cancelled()" in upload["if"]


def test_a_push_to_main_cannot_cancel_a_run_before_it_files():
    text = (_REPO / ".github" / "workflows" / "gate-corpus.yml").read_text(encoding="utf-8")
    assert "cancel-in-progress: ${{ github.event_name == 'pull_request' }}" in text


def test_the_issue_job_does_not_install_or_run_the_tests():
    run = "\n".join(s.get("run", "") for s in _jobs()["file-issue"]["steps"])
    assert "gate_corpus_issue.py" in run
    assert "pip" not in run and "pytest" not in run and "mutation_sweep" not in run
