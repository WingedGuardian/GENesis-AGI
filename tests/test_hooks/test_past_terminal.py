"""The past-round-4 rules (#4133 PR 1b-i): Prediction, Prediction-held, the
end-now rule and the two-failure send-back, read from the budget's per-key
floor flags. One test per row of the design's writer-state table."""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import review_findings as rf  # noqa: E402
import review_late as rl  # noqa: E402
import review_reflection as rr  # noqa: E402

CUT = "2026-11-01T00:00:00+00:00"
T0 = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
HEAD = "a" * 40
P1 = "**<sub><sub>![P1 Badge](https://img.shields.io/badge/P1-orange)</sub></sub>  Bug**"
P2 = "**<sub><sub>![P2 Badge](https://img.shields.io/badge/P2-yellow)</sub></sub>  Nit**"
CODEX = rf.CODEX_LOGIN


def _rec(number, *, blocking=False, opened=None, at=None, key="c1", head=None):
    opened = T0 if opened is None else opened
    at = opened if at is None else at
    return rr.RoundRecord(head or f"{number:040x}", number, opened, ((at, blocking, key),))


def _records(*recs):
    return {r.head: r for r in recs}


def _reflection(*, decision="fix-instances", fix=True, prediction=None, confidence=None, held=None):
    disposition = "fix-now test=1" if fix else "file issue=#12"
    lines = [
        f"Round-reflection: keys=c1 head={HEAD}",
        "Class: unchecked subprocess result = 1",
        "Premise: LEAD",
        "Premise-why: the change routes around an existing helper that already exists",
        "Impossible: route every call through one checked helper and delete the rest",
        "Scope: covered: the key round-trips (tests/test_x.py)",
        f"Decision: {decision}",
        "Decision-why: the same mistake appears twice in two different files",
        f"Disposition: c1 {disposition}",
    ]
    if prediction is not None:
        lines.append(f"Prediction: merges at round {prediction} because the class is closed now")
    if confidence is not None:
        lines.append(f"Confidence: {confidence}% falsified-by any new finding in that class")
    if held is not None:
        lines.append(f"Prediction-held: {held} round four drew only minor findings")
    lines.append("Escalate: no")
    return rr.parse("\n".join(lines) + "\n")


def _problems(parsed, record, records, *, at, gate=False):
    return rr.past_terminal_problems(
        parsed, record, records, gate_lane=gate, made_at=at, cutover=CUT
    )


def _ladder(n, *, blocking_at=(), opened_step=timedelta(hours=2)):
    """Rounds 1..n, one per two hours from T0; ``blocking_at`` rounds drew a P1."""
    recs = [
        _rec(i, blocking=i in blocking_at, opened=T0 + (i - 1) * opened_step)
        for i in range(1, n + 1)
    ]
    return recs[-1], _records(*recs)


def _late(n):
    """A time after round n settled (its first review + 30 min)."""
    return T0 + (n - 1) * timedelta(hours=2) + timedelta(hours=1)


# -- the grammar --------------------------------------------------------------


def test_the_three_fields_parse():
    got = _reflection(prediction=5, confidence=97, held="no")
    assert got.ok, got.problems
    assert (got.prediction_round, got.confidence, got.held, got.fix_now) == (5, 97, "no", True)
    assert not _reflection(fix=False).fix_now


# -- rows of the table --------------------------------------------------------


def test_before_round_four_or_on_the_gate_lane_nothing_changes():
    record, records = _ladder(3)
    assert _problems(_reflection(), record, records, at=_late(3)) == []
    record, records = _ladder(6)
    assert _problems(_reflection(), record, records, at=_late(6), gate=True) == []


def test_a_round_opened_before_the_cutover_keeps_its_old_rules():
    old = T0 - timedelta(days=30)
    recs = [_rec(i, opened=old + i * timedelta(hours=1)) for i in range(1, 6)]
    assert _problems(_reflection(), recs[-1], _records(*recs), at=_late(6)) == []


def test_round_four_fixing_needs_the_next_round_and_high_confidence():
    record, records = _ladder(4)
    at = _late(4)
    good = _reflection(prediction=5, confidence=95)
    assert _problems(good, record, records, at=at) == []
    for bad in (
        _reflection(),
        _reflection(prediction=6, confidence=99),
        _reflection(prediction=5, confidence=94),
    ):
        assert _problems(bad, record, records, at=at), bad


def test_round_four_filing_everything_or_sending_back_owes_no_prediction():
    record, records = _ladder(4)
    assert _problems(_reflection(fix=False), record, records, at=_late(4)) == []
    assert _problems(_reflection(decision="send-back"), record, records, at=_late(4)) == []


def test_round_five_with_no_floor_finding_ends_the_loop():
    """The #3004 rule (owner 132a2135): no floor finding, no fix-now."""
    record, records = _ladder(5)
    at = _late(5)
    fixing = _reflection(prediction=6, confidence=99, held="yes")
    assert any("loop ends here" in p for p in _problems(fixing, record, records, at=at))
    filed = _reflection(fix=False, held="yes")
    assert _problems(filed, record, records, at=at) == []


def test_round_five_with_a_floor_finding_may_fix_and_must_say_no():
    record, records = _ladder(5, blocking_at={5})
    at = _late(5)
    assert (
        _problems(_reflection(prediction=6, confidence=99, held="no"), record, records, at=at) == []
    )
    wrong = _problems(_reflection(prediction=6, confidence=99, held="yes"), record, records, at=at)
    assert any("drew floor findings (c1)" in p for p in wrong)


def test_an_honest_no_is_admitted_with_nothing_blocking():
    record, records = _ladder(5)
    assert _problems(_reflection(fix=False, held="no"), record, records, at=_late(5)) == []


def test_yes_waits_for_the_round_to_settle():
    record, records = _ladder(5)
    early = T0 + 4 * timedelta(hours=2) + timedelta(minutes=10)
    got = _problems(_reflection(fix=False, held="yes"), record, records, at=early)
    assert any("wait until" in p for p in got)
    assert _problems(_reflection(fix=False, held="no"), record, records, at=early) == []


def test_an_unreadable_level_cannot_be_held_and_does_not_end_the_loop():
    recs = [_rec(i, opened=T0 + (i - 1) * timedelta(hours=2)) for i in range(1, 5)]
    recs.append(_rec(5, blocking=None, opened=T0 + 4 * timedelta(hours=2)))
    record, records = recs[-1], _records(*recs)
    at = _late(5)
    assert any(
        "cannot be verified" in p
        for p in _problems(
            _reflection(prediction=6, confidence=99, held="yes"), record, records, at=at
        )
    )
    assert (
        _problems(_reflection(prediction=6, confidence=99, held="no"), record, records, at=at) == []
    )


def test_round_five_needs_the_held_line():
    record, records = _ladder(5, blocking_at={5})
    got = _problems(_reflection(prediction=6, confidence=99), record, records, at=_late(5))
    assert any("Prediction-held" in p for p in got)


def test_two_failed_predictions_leave_only_send_back():
    record, records = _ladder(6, blocking_at={5, 6})
    at = _late(6)
    got = _problems(_reflection(prediction=7, confidence=99, held="no"), record, records, at=at)
    assert any("only decision left" in p for p in got)
    assert _problems(_reflection(decision="send-back", held="no"), record, records, at=at) == []


def test_a_floor_finding_after_settle_does_not_count_as_a_failure():
    """F reads each round at min(T, its settle time): a late finding cannot
    turn an earlier round into a failed prediction after the fact."""
    r5_open = T0 + 4 * timedelta(hours=2)
    r5 = rr.RoundRecord(
        "5" * 40, 5, r5_open, ((r5_open + timedelta(hours=5), True, "c9"), (r5_open, False, "c1"))
    )
    r6 = _rec(6, blocking=True, opened=r5_open + timedelta(hours=2))
    records = _records(
        *[_rec(i, opened=T0 + (i - 1) * timedelta(hours=2)) for i in range(1, 5)], r5, r6
    )
    assert rr.prediction_failures(records, r5_open + timedelta(hours=9), CUT) == 1


def test_a_finding_after_the_reflection_never_changes_its_answer():
    r5_open = T0 + 4 * timedelta(hours=2)
    record = rr.RoundRecord(
        "5" * 40, 5, r5_open, ((r5_open, False, "c1"), (r5_open + timedelta(hours=3), True, "c2"))
    )
    records = _records(
        *[_rec(i, opened=T0 + (i - 1) * timedelta(hours=2)) for i in range(1, 5)], record
    )
    at = r5_open + timedelta(hours=1)
    assert _problems(_reflection(fix=False, held="yes"), record, records, at=at) == []


def test_send_back_with_no_passes_in_every_state():
    """No dead end: whatever the facts, send-back with an honest no passes."""
    for blocking_at in (set(), {5}, {5, 6}):
        for n in (4, 5, 6):
            record, records = _ladder(n, blocking_at=blocking_at)
            for fix in (True, False):
                got = _problems(
                    _reflection(decision="send-back", fix=fix, held="no"),
                    record,
                    records,
                    at=_late(n),
                )
                assert got == [], (n, blocking_at, fix, got)


def test_an_unreadable_commit_time_is_named():
    record, records = _ladder(5)
    assert _problems(_reflection(), record, records, at=None)


# -- loop_ended -----------------------------------------------------------------


def test_the_loop_ends_at_a_round_five_with_no_floor_finding():
    _, records = _ladder(5)
    assert rr.loop_ended(records, gate_lane=False, now=_late(5), cutover=CUT) is True
    _, records = _ladder(5, blocking_at={5})
    assert rr.loop_ended(records, gate_lane=False, now=_late(5), cutover=CUT) is False
    _, records = _ladder(4)
    assert rr.loop_ended(records, gate_lane=False, now=_late(4), cutover=CUT) is False
    _, records = _ladder(5)
    assert rr.loop_ended(records, gate_lane=True, now=_late(5), cutover=CUT) is False
    assert rr.loop_ended(None, gate_lane=False, now=_late(5), cutover=CUT) is None


def test_an_unreadable_level_never_ends_the_loop():
    recs = [_rec(i, opened=T0 + (i - 1) * timedelta(hours=2)) for i in range(1, 5)]
    recs.append(_rec(5, blocking=None, opened=T0 + 4 * timedelta(hours=2)))
    assert rr.loop_ended(_records(*recs), gate_lane=False, now=_late(5), cutover=CUT) is None


# -- is_blocking and the budget's records -------------------------------------


@pytest.mark.parametrize(
    "login, body, want",
    [
        (CODEX, P1, True),
        (CODEX, P2, False),
        (CODEX, "no badge here", None),
        ("coderabbitai[bot]", "_⚠️ Potential issue_ | _🟠 Major_\n\nbody", True),
        ("coderabbitai[bot]", "_⚠️ Potential issue_ | _🟡 Minor_\n\nbody", False),
        ("coderabbitai[bot]", "no header", None),
        ("someone-else[bot]", P1, None),
    ],
)
def test_is_blocking_reads_the_floor(login, body, want):
    assert rf.is_blocking(login, body) is want


def test_round_records_read_the_budget_findings():
    budget = {
        "rounds": [
            {"head": "a" * 40, "opened_at": T0.isoformat()},
            {"head": "b" * 40, "opened_at": (T0 + timedelta(hours=1)).isoformat()},
        ],
        "findings": [
            {
                "round_head": "a" * 40,
                "submitted_at": T0.isoformat(),
                "keys": ["c1"],
                "blocking": [True],
            },
            {
                "round_head": "b" * 40,
                "submitted_at": (T0 + timedelta(hours=1)).isoformat(),
                "keys": ["c2", "c3"],
                "blocking": [False],
            },  # wrong length: read as unknown
        ],
    }
    records = rl.round_records(budget)
    assert records["a" * 40].number == 1 and records["b" * 40].number == 2
    assert records["a" * 40].blocking(T0)[0] is True
    assert records["b" * 40].blocking(T0 + timedelta(hours=2))[0] is None
    assert rl.round_records({"rounds": [{"head": "a" * 40}]}) is None


# -- Rule R: the commit gate after the loop ended ------------------------------

import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "past_terminal_gate", ROOT / "scripts" / "review_enforcement_commit.py"
)
_gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_gate)


def _ended_budget(*, blocking_at=(), gate=False):
    now = datetime.now(UTC)
    start = now - timedelta(hours=20)
    rounds, findings = [], []
    for n in range(1, 6):
        head = f"{n:040x}"
        opened = start + (n - 1) * timedelta(hours=2)
        rounds.append({"head": head, "opened_at": opened.isoformat()})
        findings.append(
            {"round_head": head, "submitted_at": opened.isoformat(), "keys": [f"c{n}"],
             "blocking": [n in blocking_at]}
        )
    return {
        "status": "ok", "pr": 7, "repo": "o/r", "count": 5, "gate_surface": gate,
        "current_head": "e" * 40, "rounds": rounds, "findings": findings,
    }, (start - timedelta(hours=1)).isoformat()


def _rule(monkeypatch, budget, cutover, *, merge_of_main=False, mode=None):
    monkeypatch.setattr(rr, "PAST_TERMINAL_CUTOVER_ISO", cutover)
    monkeypatch.setattr(rr, "owed_from_budget", lambda *a, **k: {"owed": []})
    monkeypatch.setattr(_gate, "_note_unreflected_round", lambda *a, **k: None)
    monkeypatch.setattr(_gate, "_concludes_merge_of_main", lambda *a, **k: merge_of_main)
    if mode:
        monkeypatch.setenv(_gate._REFLECTION_SEAM, mode)
    import time

    return _gate._reflection_rule(
        [], [["git", "commit", "-m", "x"]], 'git commit -m "x"', ".", budget,
        hook_deadline=time.monotonic() + 9,
    )


def test_a_content_commit_after_the_loop_ended_is_denied(monkeypatch):
    budget, cutover = _ended_budget()
    verdict, message = _rule(monkeypatch, budget, cutover)
    assert verdict == "deny" and "review loop of PR #7 is over" in message


def test_a_floor_finding_in_the_newest_round_keeps_fixing_open(monkeypatch):
    budget, cutover = _ended_budget(blocking_at={5})
    assert _rule(monkeypatch, budget, cutover)[0] == "continue"


def test_the_gate_lane_and_a_merge_of_main_are_not_refused(monkeypatch):
    budget, cutover = _ended_budget(gate=True)
    assert _rule(monkeypatch, budget, cutover)[0] == "continue"
    budget, cutover = _ended_budget()
    assert _rule(monkeypatch, budget, cutover, merge_of_main=True)[0] == "continue"


def test_rounds_before_the_cutover_never_end_the_loop(monkeypatch):
    budget, _ = _ended_budget()
    later = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    assert _rule(monkeypatch, budget, later)[0] == "continue"


def test_advisory_mode_reports_and_allows(monkeypatch):
    budget, cutover = _ended_budget()
    _gate._ADVISORIES.clear()
    assert _rule(monkeypatch, budget, cutover, mode="advisory")[0] == "continue"
    assert any("review loop" in a for a in _gate._ADVISORIES)


def test_admission_names_the_end_now_refusal(monkeypatch, tmp_path):
    """Admission applies the same rules from the budget it holds, so a fix-now
    past the loop's end is a named deny there, not a key left silently owed."""
    budget, cutover = _ended_budget()
    monkeypatch.setattr(rr, "PAST_TERMINAL_CUTOVER_ISO", cutover)
    message = tmp_path / "reflection.md"
    lines = [
        f"Round-reflection: keys=c5 head={budget['current_head']}",
        "Class: unchecked subprocess result = 1",
        "Premise: LEAD",
        "Premise-why: the change routes around an existing helper that already exists",
        "Impossible: route every call through one checked helper and delete the rest",
        "Scope: covered: the key round-trips (tests/test_x.py)",
        "Decision: fix-instances",
        "Decision-why: the same mistake appears twice in two different files",
        "Disposition: c5 fix-now test=1",
        "Prediction: merges at round 6 because the class is closed now",
        "Confidence: 99% falsified-by any new finding in that class",
        "Prediction-held: yes round five drew only minor findings again",
        "Escalate: no",
    ]
    message.write_text("\n".join(lines) + "\n")
    monkeypatch.setattr(_gate, "_exemption_shape_ok", lambda *a: True)
    monkeypatch.setattr(_gate, "_reflection_message_file", lambda seg: str(message))
    monkeypatch.setattr(_gate, "_staged_files", lambda *a, **k: [])
    monkeypatch.setattr(_gate, "_integration_in_progress", lambda *a, **k: False)
    import time

    problem = _gate._reflection_problem(
        [["git"]], "git commit", str(tmp_path), budget, rr, deadline=time.monotonic() + 9
    )
    assert problem and "loop ends here" in problem


def _git(repo, *args):
    import subprocess

    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout


def _merge_repo(tmp_path):
    """A PR branch ``feat`` and a main that moved on, with origin/HEAD set."""
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "a").write_text("a\n")
    _git(repo, "add", "a")
    _git(repo, "commit", "-qm", "base")
    _git(repo, "checkout", "-qb", "feat")
    (repo / "f").write_text("feature\n")
    _git(repo, "add", "f")
    _git(repo, "commit", "-qm", "feature")
    _git(repo, "checkout", "-q", "main")
    (repo / "m").write_text("main moved\n")
    _git(repo, "add", "m")
    _git(repo, "commit", "-qm", "main moves")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    _git(repo, "checkout", "-q", "feat")
    return repo


def _commit_segs(command):
    from shell_parse import analyze_checked  # noqa: PLC0415

    return analyze_checked(command)[0]


def test_a_plain_merge_of_main_is_allowed_after_the_loop_ended(tmp_path):
    repo = _merge_repo(tmp_path)
    segs = _commit_segs("git commit --no-edit")
    assert _gate._concludes_merge_of_main(str(repo), rr, segs) is False  # no merge yet
    _git(repo, "merge", "-q", "--no-commit", "--no-ff", "main")
    assert _gate._concludes_merge_of_main(str(repo), rr, segs) is True
    assert _gate._concludes_merge_of_main(str(repo), rr, _commit_segs("git commit -a --no-edit")) is False


def test_a_fix_riding_a_merge_of_main_is_not_allowed(tmp_path):
    repo = _merge_repo(tmp_path)
    _git(repo, "merge", "-q", "--no-commit", "--no-ff", "main")
    (repo / "f").write_text("feature, fixed\n")
    _git(repo, "add", "f")
    assert _gate._concludes_merge_of_main(str(repo), rr, _commit_segs("git commit --no-edit")) is False


def test_a_merge_of_another_branch_is_not_a_merge_of_main(tmp_path):
    repo = _merge_repo(tmp_path)
    _git(repo, "checkout", "-qb", "other", "main~1")
    (repo / "o").write_text("other\n")
    _git(repo, "add", "o")
    _git(repo, "commit", "-qm", "other")
    _git(repo, "checkout", "-q", "feat")
    _git(repo, "merge", "-q", "--no-commit", "--no-ff", "other")
    assert _gate._concludes_merge_of_main(str(repo), rr, _commit_segs("git commit --no-edit")) is False


def _two_keys(c1, c2, *, held="yes"):
    lines = [
        f"Round-reflection: keys=c1,c2 head={HEAD}",
        "Class: unchecked subprocess result = 2",
        "Premise: LEAD",
        "Premise-why: the change routes around an existing helper that already exists",
        "Impossible: route every call through one checked helper and delete the rest",
        "Scope: covered: the key round-trips (tests/test_x.py)",
        "Decision: fix-instances",
        "Decision-why: the same mistake appears twice in two different files",
        f"Disposition: c1 {c1}",
        f"Disposition: c2 {c2}",
        "Prediction: merges at round 7 because the class is closed now",
        "Confidence: 99% falsified-by any new finding in that class",
        f"Prediction-held: {held} round six drew a floor finding",
        "Escalate: no",
    ]
    return rr.parse("\n".join(lines) + "\n")


def test_end_now_is_judged_per_key():
    """Audit SF-2: filing round 5's key while fixing round 6's floor finding is
    allowed; fixing round 5's key is not."""
    r5_open, r6_open = T0 + 8 * timedelta(hours=1), T0 + 10 * timedelta(hours=1)
    earlier = [_rec(i, opened=T0 + (i - 1) * timedelta(hours=1)) for i in range(1, 5)]
    r5 = rr.RoundRecord("5" * 40, 5, r5_open, ((r5_open, False, "c1"),))
    r6 = rr.RoundRecord("6" * 40, 6, r6_open, ((r6_open, True, "c2"),))
    records = _records(*earlier, r5, r6)
    at = r6_open + timedelta(hours=1)
    ok = _two_keys("file issue=#12", "fix-now test=1", held="no")
    assert _problems(ok, r5, records, at=at) == []
    bad = _two_keys("fix-now test=1", "fix-now test=1", held="no")
    assert any("loop ends here" in p for p in _problems(bad, r5, records, at=at))


def test_an_older_key_cannot_be_fixed_once_the_loop_ended():
    """Audit SF-1: a round-4 key answered after round 5 ended the loop: a
    fix-now would be admitted and then refused at the commit, so it is
    refused here."""
    record, records = _ladder(5)
    r4 = [r for r in records.values() if r.number == 4][0]
    got = _problems(_reflection(prediction=6, confidence=99), r4, records, at=_late(5))
    assert any("review loop is over" in p for p in got)
    assert _problems(_reflection(fix=False), r4, records, at=_late(5)) == []


def test_one_unplaceable_round_turns_the_rules_off_for_the_pr():
    """Audit SF-3 / SF-4: a tie leaves the newest round and the round count
    unknown, so nothing is refused on a guess."""
    same = T0.isoformat()
    budget = {
        "rounds": [{"head": "a" * 40, "opened_at": same}, {"head": "b" * 40, "opened_at": same}],
        "findings": [],
    }
    assert rl.round_records(budget) is None
    assert rr.loop_ended(None, gate_lane=False, now=T0, cutover=CUT) is None


def test_a_round_opened_after_the_reflection_is_not_its_newest():
    """A round opened after T did not exist at T: the loop's end is judged
    against the rounds opened by then."""
    _, records = _ladder(5)
    r6 = _rec(6, blocking=False, opened=T0 + 20 * timedelta(hours=1))
    records = {**records, r6.head: r6}
    _, with_floor = _ladder(5, blocking_at={5})
    late6 = {**with_floor, r6.head: r6}
    assert rr.loop_ended(late6, gate_lane=False, now=_late(5), cutover=CUT) is False
    assert rr.loop_ended(late6, gate_lane=False, now=r6.opened_at + timedelta(hours=1), cutover=CUT) is True
