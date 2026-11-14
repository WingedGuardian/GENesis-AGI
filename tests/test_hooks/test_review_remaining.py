"""Remaining findings are advisory, with the same evidence checks as coverage."""

import copy
import importlib.util
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests.test_hooks.test_review_reflection import (
    _commit_reflection,
    _fix,
    _git,
    _reflection,
)
from tests.test_hooks.test_review_reflection import (
    repo as _repo_fixture,
)

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import review_reflection as rr  # noqa: E402
import review_remaining as remaining  # noqa: E402


@pytest.fixture
def repo(tmp_path):
    return _repo_fixture.__wrapped__(tmp_path)


@pytest.fixture
def guard():
    spec = importlib.util.spec_from_file_location("remaining_guard", ROOT / "scripts/hooks/git_push_guard.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _budget():
    return {
        "status": "ok", "count": 2, "gate_surface": False,
        "round_state": "complete", "trend": "rising",
        "rounds": [{"head": "a" * 40, "findings": 1}, {"head": "b" * 40, "findings": 3}],
    }


def test_display_separates_raw_and_remaining_trends(guard):
    budget = _budget()
    budget["remaining_report"] = {"status": "ok", "trend": "flat", "rounds": [
        {"raw_findings": 1, "no_defect": 0, "remaining_findings": 1},
        {"raw_findings": 3, "no_defect": 2, "remaining_findings": 1},
    ]}
    row = guard._format_rounds(budget)
    assert "raw findings per round 1→3" in row
    assert "no-defect per round 0→2" in row
    assert "remaining findings per round 1→1 (flat)" in row
    assert budget["trend"] == "rising" and budget["count"] == 2


@pytest.mark.parametrize("report", [None, {"status": "unknown", "reason": "unreadable citation"}])
def test_unchecked_history_displays_unknown_not_zero(guard, report):
    budget = _budget()
    budget["remaining_report"] = report
    row = guard._format_rounds(budget)
    assert "remaining findings UNKNOWN" in row
    assert "no-defect per round 0" not in row


STAMP = "2026-11-01T00:00:00+00:00"


def _case(repo, tmp_path, monkeypatch, no_defect):
    path, head = repo
    keys = ["c1", "r7:3", "i9"]
    dispositions = [
        f"Disposition: {key} no-defect {'reply' if key.startswith('c') else 'comment'}={40 + i}"
        if key == no_defect else f"Disposition: {key} fix-now test=1"
        for i, key in enumerate(keys)
    ]
    _commit_reflection(path, tmp_path, _reflection(keys=keys, head=head, dispositions=dispositions))
    budget = {"status": "ok", "count": 1, "current_head": head, "round_state": "open",
              "gate_surface": False, "body": "", "reflection_keys": "ok", "late_state": "ok",
              "open_keys": keys, "marks": [], "commits": [{"sha": head, "parents": []}],
              "rounds": [{"head": head, "findings": 5, "opened_at": STAMP,
                          "reviews": [{"submitted_at": STAMP, "finding_keys": keys}]}],
              "findings": [{"head": head, "round_head": head, "submitted_at": STAMP,
                            "keys": keys + keys, "late": False}]}
    monkeypatch.setattr(rr, "pr_identity", lambda cwd: ("o/r", 8))
    original = rr._run
    calls = []
    def run(argv, **kw):
        if argv[0] != "gh":
            return original(argv, **kw)
        calls.append(argv[-1])
        citation = int(argv[-1].rsplit("/", 1)[1])
        key = keys[citation - 40]
        record = dict(id=citation, author_association="OWNER", in_reply_to_id=1,
                      body=f"No-defect: {key}\nEvidence: existing path covers this.",
                      created_at=STAMP, updated_at=STAMP,
                      pull_request_url="https://api.github.com/repos/o/r/pulls/8",
                      issue_url="https://api.github.com/repos/o/r/issues/8")
        return 0, json.dumps(record), ""
    monkeypatch.setattr(rr, "_run", run)
    return path, budget, calls


@pytest.mark.parametrize("key,weight", [("c1", 1), ("r7:3", 3), ("i9", 1), (None, 0)])
def test_validated_counts_weight_groups_and_deduplicate_sources(repo, tmp_path, monkeypatch, key, weight):
    path, budget, calls = _case(repo, tmp_path, monkeypatch, key)
    before = copy.deepcopy(budget)
    result = remaining.remaining_report(str(path), "o/r", 8, budget, deadline=time.monotonic() + 30)
    assert result["status"] == "ok", result
    assert result["rounds"][0] == {"head": budget["current_head"], "raw_findings": 5,
                                   "no_defect": weight, "remaining_findings": 5 - weight}
    assert len(calls) == (0 if key is None else 1)
    assert budget == before  # counts, enforcement and raw trend are never rewritten


@pytest.mark.parametrize("fault", ["foreign", "absent-head", "citation", "keys", "deadline"])
def test_unreadable_or_inapplicable_evidence_is_unknown(repo, tmp_path, monkeypatch, fault):
    path, budget, calls = _case(repo, tmp_path, monkeypatch, "r7:3")
    deadline = time.monotonic() + 30
    if fault == "foreign":
        monkeypatch.setattr(rr, "pr_identity", lambda cwd: ("o/r", 99))
    elif fault == "absent-head":
        budget["current_head"] = "f" * 40
    elif fault == "citation":
        previous = rr._run
        monkeypatch.setattr(rr, "_run", lambda argv, **kw: (1, "", "offline") if argv[0] == "gh" else previous(argv, **kw))
    elif fault == "keys":
        budget["rounds"][0]["reviews"][0]["finding_keys"] = [None]
    else:
        deadline = time.monotonic() - 1
    result = remaining.remaining_report(str(path), "o/r", 8, budget, deadline=deadline)
    assert result["status"] == "unknown"
    assert "rounds" not in result  # unknown must not become a zero subtraction


def test_open_round_is_unknown_when_the_gate_could_not_read_its_keys(repo, tmp_path, monkeypatch):
    """owed_state credits nothing unless reflection_keys is "ok"; the report must
    not show a no-defect credit the gate would never give."""
    path, budget, calls = _case(repo, tmp_path, monkeypatch, "r7:3")
    budget["reflection_keys"] = "unknown"
    result = remaining.remaining_report(str(path), "o/r", 8, budget, deadline=time.monotonic() + 30)
    assert result["status"] == "unknown"
    assert "rounds" not in result


def test_open_round_is_unknown_when_the_gate_late_debt_is_unknown(repo, tmp_path, monkeypatch):
    """owed_state credits the open round only when the late debt is known; an
    unknown late debt (here forced) must make the report unknown too."""
    path, budget, calls = _case(repo, tmp_path, monkeypatch, "r7:3")
    monkeypatch.setattr(remaining.late, "late_owed", lambda *a, **kw: (None, None))
    result = remaining.remaining_report(str(path), "o/r", 8, budget, deadline=time.monotonic() + 30)
    assert result["status"] == "unknown"
    assert "rounds" not in result


def test_open_round_is_dated_as_the_gate_dates_it(repo, tmp_path, monkeypatch):
    """The report dates the open round from the gate's own state (owed_state),
    never from the producer's opened_at, so the two cannot drift apart."""
    path, budget, calls = _case(repo, tmp_path, monkeypatch, "r7:3")
    budget["rounds"][0]["opened_at"] = "2026-10-01T00:00:00+00:00"
    seen = []
    real = rr.coverage_details

    def spy(*args, **kw):
        seen.append(kw["round_started"])
        return real(*args, **kw)

    monkeypatch.setattr(rr, "coverage_details", spy)
    result = remaining.remaining_report(str(path), "o/r", 8, budget, deadline=time.monotonic() + 30)
    assert result["status"] == "ok", result
    gate = rr.owed_state(budget, set(), now=datetime.now(UTC), cutover=None)["round_started"]
    assert seen == [gate] == [STAMP]


def test_old_round_uses_its_own_validated_context(repo, tmp_path, monkeypatch):
    path, budget, calls = _case(repo, tmp_path, monkeypatch, "r7:3")
    old = budget["current_head"]
    _fix(path, "next round\n")
    new = _git(path, "rev-parse", "HEAD").strip()
    stamp = "2026-11-02T00:00:00+00:00"
    budget.update(current_head=new, count=2, open_keys=["c2"])
    budget["commits"].append({"sha": new, "parents": [old]})
    budget["rounds"].append({"head": new, "findings": 1, "opened_at": stamp,
                             "reviews": [{"submitted_at": stamp, "finding_keys": ["c2"]}]})
    budget["findings"].append({"head": new, "round_head": new, "submitted_at": stamp, "keys": ["c2"], "late": False})
    result = remaining.remaining_report(str(path), "o/r", 8, budget, deadline=time.monotonic() + 30)
    assert result["status"] == "ok", result
    assert [row["no_defect"] for row in result["rounds"]] == [3, 0]
    assert [row["remaining_findings"] for row in result["rounds"]] == [2, 1]


def test_late_keys_use_the_late_coverage_result(repo, tmp_path, monkeypatch):
    path, budget, calls = _case(repo, tmp_path, monkeypatch, "r7:3")
    seen = []
    monkeypatch.setattr(remaining.late, "late_findings", lambda *a, **kw: [{"key": "r7:3"}])
    def covered(*a, **kw):
        seen.append(kw)
        return {"r7:3": rr.Disposition("no-defect", 41)}
    monkeypatch.setattr(remaining.late, "coverage_details", covered)
    result = remaining.remaining_report(str(path), "o/r", 8, budget, deadline=time.monotonic() + 30)
    assert result["rounds"][0]["no_defect"] == 3
    assert seen[0]["repo"] == "o/r" and seen[0]["number"] == 8
    assert calls == []  # normal coverage must not revalidate an ineligible late key


def test_late_only_head_is_not_part_of_the_raw_round_population(repo, tmp_path, monkeypatch):
    from tests.test_hooks.test_review_late import CUT, _at, _late_budget, _rv

    path, initial, calls = _case(repo, tmp_path, monkeypatch, "c1")
    head = initial["current_head"]
    base = _git(path, "rev-parse", head + "^").strip()
    _fix(path, "late-only head\n")
    late_head = _git(path, "rev-parse", "HEAD").strip()
    _fix(path, "tip\n")
    tip = _git(path, "rev-parse", "HEAD").strip()
    budget = _late_budget(path, base, tip, [
        _rv(head, _at(0), 1), _rv(tip, _at(70)), _rv(late_head, _at(90), 9),
    ])
    budget["body"] = ""
    monkeypatch.setattr(rr, "REFLECTION_CUTOVER_ISO", CUT)
    assert len(budget["rounds"]) == 1 and budget["rounds"][0]["findings"] == 1
    assert {source["round_head"] for source in budget["findings"]} == {head}
    assert {source["head"] for source in budget["findings"]} == {head, late_head}
    report = remaining.remaining_report(str(path), "o/r", 8, budget, deadline=time.monotonic() + 30)
    assert report["status"] == "ok", report
    assert report["rounds"] == [{"head": head, "raw_findings": 1, "no_defect": 1,
                                  "remaining_findings": 0}]
    assert calls == ["repos/o/r/pulls/comments/40"]
