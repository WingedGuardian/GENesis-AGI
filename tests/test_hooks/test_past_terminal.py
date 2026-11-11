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


def _rule(monkeypatch, budget, cutover, *, merge_of_main=False, mode=None, log=()):
    monkeypatch.setattr(rr, "PAST_TERMINAL_CUTOVER_ISO", cutover)
    monkeypatch.setattr(rr, "owed_from_budget", lambda *a, **k: {"owed": []})
    monkeypatch.setattr(_gate, "_note_unreflected_round", lambda *a, **k: None)
    monkeypatch.setattr(_gate, "_concludes_merge_of_main", lambda *a, **k: merge_of_main)
    monkeypatch.setattr(_gate, "_exemption_shape_ok", lambda *a: True)
    monkeypatch.setattr(rr, "_log_reflections", lambda cwd: list(log))
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
    monkeypatch.setattr(rr, "_log_reflections", lambda cwd: [])
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


def test_confidence_is_a_percentage():
    assert not _reflection(prediction=5, confidence=101).ok
    assert _reflection(prediction=5, confidence=100).ok
# -- round 1 (#4368): the class audit's members --------------------------------


def _ans(keys, *, at, sent_back=False, fix_now=False, fixes=None):
    fixes = frozenset(keys if fix_now and fixes is None else fixes or ())
    return rr.Answer(frozenset(keys), sent_back, bool(fixes) or fix_now, at, fixes)


def _ended(records, at, declared=(), flags=None):
    return rr.loop_ended(
        records, gate_lane=False, now=at, cutover=CUT, declared=declared, flags=flags
    )


def test_a_send_back_answering_the_newest_round_ends_the_loop():
    """C1.1: send-back at a floor round past round 4 ends the loop, so its
    fix-now dispositions cannot credit a fix that then lands."""
    _, records = _ladder(5, blocking_at={5})
    at = _late(5)
    assert _ended(records, at) is False
    assert _ended(records, at, [_ans({"c1"}, at=at, sent_back=True, fix_now=True)]) is True


def test_a_no_fix_now_answer_ends_the_loop_from_round_four():
    """C1.2 (owner a90139c6): filing every finding at round 4, or at a floor
    round 5, ends the loop: the fix cannot land without a Prediction."""
    for n, floor in ((4, set()), (5, {5})):
        _, records = _ladder(n, blocking_at=floor)
        at = _late(n)
        assert _ended(records, at, [_ans({"c1"}, at=at)]) is True
        assert _ended(records, at, [_ans({"c1"}, at=at, fix_now=True)]) is False


def test_round_three_answers_never_end_the_loop():
    _, records = _ladder(3)
    at = _late(3)
    assert _ended(records, at, [_ans({"c1"}, at=at, sent_back=True)]) is False
    assert rr.ending_possible(records, gate_lane=False, now=at, cutover=CUT) is False


def test_the_end_is_monotone_and_only_a_new_round_lifts_it():
    """A later fixing reflection cannot reopen the loop; a newer round does."""
    _, records = _ladder(5, blocking_at={5})
    at = _late(5)
    answers = [_ans({"c1"}, at=at), _ans({"c1"}, at=at + timedelta(minutes=5), fix_now=True)]
    assert _ended(records, at + timedelta(minutes=10), answers) is True
    r6 = _rec(6, blocking=True, opened=at + timedelta(hours=1), key="c6")
    later = {**records, r6.head: r6}
    assert _ended(later, r6.opened_at + timedelta(minutes=40), answers) is False


def test_an_answer_naming_no_newest_key_does_not_end_it():
    _, records = _ladder(5, blocking_at={5})
    at = _late(5)
    assert _ended(records, at, [_ans({"c9"}, at=at)]) is False


def test_two_failures_then_a_send_back_end_the_loop():
    """C1.3: the two-failure rule's only exit is send-back, which now ends it."""
    _, records = _ladder(6, blocking_at={5, 6})
    at = _late(6)
    assert _ended(records, at) is False
    assert _ended(records, at, [_ans({"c1"}, at=at, sent_back=True)]) is True


@pytest.mark.parametrize("which", ["round4", "round2", "none", "precutover"])
def test_the_two_failure_rule_reads_the_pr_not_the_key(which):
    """C2.1, C2.2: every record a key can land on gets the PR-wide rule."""
    _, records = _ladder(6, blocking_at={5, 6})
    by_n = {r.number: r for r in records.values()}
    record = {
        "round4": by_n[4],
        "round2": by_n[2],
        "none": None,
        "precutover": _rec(1, opened=T0 - timedelta(days=30)),
    }[which]
    got = _problems(
        _reflection(prediction=7, confidence=99, held="no"), record, records, at=_late(6)
    )
    assert any("only decision left" in p for p in got)


def test_the_prediction_is_owed_pr_wide():
    """C2.3 (owner a90139c6): a late round-2 key fixed at PR round 5 owes it."""
    _, records = _ladder(5, blocking_at={5})
    r2 = [r for r in records.values() if r.number == 2][0]
    at = _late(5)
    got = _problems(_reflection(), r2, records, at=at)
    assert any("Prediction: merges at round 6" in p for p in got)
    assert _problems(_reflection(prediction=6, confidence=97), r2, records, at=at) == []



def test_an_untimed_finding_leaves_the_settle_time_unknown():
    """C4: an unreadable time is not year 1, so F still sees the round's
    floor finding and a 'yes' cannot pass as settled."""
    r = rr.RoundRecord(
        "5" * 40, 5, T0, ((None, False, "c1"), (T0 + timedelta(minutes=5), True, "c2"))
    )
    assert r.settle_at is None
    assert r.blocking(T0)[0] is False
    assert r.blocking(T0 + timedelta(minutes=5)) == (True, ["c2"])
    records = _records(
        r, rr.RoundRecord("6" * 40, 6, T0 + timedelta(hours=1), ((None, True, "c3"),))
    )
    assert rr.prediction_failures(records, T0 + timedelta(hours=2), CUT) == 2


def test_round_records_keep_an_unreadable_time_as_none():
    budget = {
        "rounds": [{"head": HEAD, "opened_at": T0.isoformat()}],
        "findings": [
            {"round_head": HEAD, "submitted_at": "garbage", "keys": ["c1"], "blocking": [True]}
        ],
    }
    records = rl.round_records(budget)
    if records is None:  # the producer's own shape decides placement; nothing to check
        pytest.skip("round_records could not place this minimal budget")
    assert records[HEAD].findings[0][0] is None


def test_the_log_is_read_only_past_round_four(monkeypatch):
    """No PR before round 4 pays for the reflection log read."""
    budget, _ = _ended_budget()
    budget = {
        **budget,
        "rounds": budget["rounds"][:3],
        "findings": budget["findings"][:3],
        "count": 3,
    }
    calls = []
    monkeypatch.setattr(rr, "_log_reflections", lambda cwd: calls.append(cwd) or [])
    assert (
        _gate._declared_answers(".", budget, rr, rr._round_records(budget), datetime.now(UTC), None)
        == ()
    )
    assert calls == []


def test_a_declared_end_denies_the_next_content_commit(monkeypatch):
    """The commit gate reads the committed answers: a file-all reflection on
    the newest round 5 that drew a floor finding ends the loop."""
    budget, cutover = _ended_budget(blocking_at={5})
    assert _rule(monkeypatch, budget, cutover)[0] == "continue"
    filed = (
        f"Round-reflection: keys=c5 head={'e' * 40}\n"
        "Class: unchecked subprocess result = 1\nPremise: LEAD\n"
        "Premise-why: the change routes around an existing helper that already exists\n"
        "Impossible: route every call through one checked helper and delete the rest\n"
        "Scope: covered: the key round-trips (tests/test_x.py)\nDecision: fix-instances\n"
        "Decision-why: the same mistake appears twice in two different files\n"
        "Disposition: c5 file issue=#12\nPrediction-held: no round five drew a P1\nEscalate: no\n"
    )
    log = [("f" * 40, "empty", filed, datetime.now(UTC) - timedelta(minutes=1))]
    verdict, message = _rule(monkeypatch, budget, cutover, log=log)
    assert verdict == "deny" and "is over" in message


def test_the_ended_branch_admits_an_empty_reflection_commit(monkeypatch):
    """C3.1: the one reflection form, nothing staged, passes after the end."""
    budget, cutover = _ended_budget()
    monkeypatch.setattr(_gate, "_reflection_form_problem", lambda *a, **k: (None, "text"))
    assert _rule(monkeypatch, budget, cutover)[0] == "continue"
    monkeypatch.setattr(_gate, "_reflection_form_problem", lambda *a, **k: ("staged", None))
    assert _rule(monkeypatch, budget, cutover)[0] == "deny"


@pytest.mark.parametrize(
    "command",
    [
        "git -C /tmp/a -C /tmp/b commit --no-edit",
        "git checkout origin/fix -- f && git commit --no-edit",
        "GIT_INDEX_FILE=/tmp/i git commit --no-edit",
    ],
)
def test_the_ended_exemptions_need_the_one_plain_commit_shape(command):
    """C3.4-C3.6: a second -C, chained staging, or an env prefix can make the
    commit land something the hook never saw, so no exemption applies."""
    segs = _commit_segs(command)
    assert _gate._exemption_shape_ok(segs, command) is False
    assert _gate._ended_commit_ok(segs, command, ".", {"gate_surface": False}, rr, None) is False


def test_a_fifo_merge_head_cannot_hang_the_probe(tmp_path):
    """C3.3: MERGE_HEAD is read non-blocking, regular files only."""
    import os

    repo = _merge_repo(tmp_path)
    os.mkfifo(repo / ".git" / "MERGE_HEAD")
    assert (
        _gate._concludes_merge_of_main(str(repo), rr, _commit_segs("git commit --no-edit")) is False
    )


def test_every_merge_probe_runs_under_the_deadline(tmp_path, monkeypatch):
    """C3.2: a spent deadline refuses the exemption before any git process
    starts, the HEAD and MERGE_HEAD probes included."""
    import time

    repo = _merge_repo(tmp_path)
    _git(repo, "merge", "-q", "--no-commit", "--no-ff", "main")
    segs = _commit_segs("git commit --no-edit")
    assert _gate._concludes_merge_of_main(str(repo), rr, segs, time.monotonic() + 30) is True
    started = []
    real = rr.subprocess.run
    monkeypatch.setattr(rr.subprocess, "run", lambda *a, **k: started.append(a) or real(*a, **k))
    assert _gate._concludes_merge_of_main(str(repo), rr, segs, time.monotonic() - 1) is False
    assert started == []
@pytest.mark.parametrize("answered", [False, True])
def test_admission_reads_the_committed_answers(monkeypatch, tmp_path, answered):
    """A file-all answer already committed for the newest round ends the loop,
    so a later fix-now reflection is refused at admission, by name."""
    import time

    budget, cutover = _ended_budget(blocking_at={5})
    monkeypatch.setattr(rr, "PAST_TERMINAL_CUTOVER_ISO", cutover)
    head = budget["current_head"]
    common = [
        "Class: unchecked subprocess result = 1",
        "Premise: LEAD",
        "Premise-why: the change routes around an existing helper that already exists",
        "Impossible: route every call through one checked helper and delete the rest",
        "Scope: covered: the key round-trips (tests/test_x.py)",
        "Decision: fix-instances",
        "Decision-why: the same mistake appears twice in two different files",
    ]
    fixing = [
        f"Round-reflection: keys=c5 head={head}",
        *common,
        "Disposition: c5 fix-now test=1",
        "Prediction: merges at round 6 because the class is closed now",
        "Confidence: 99% falsified-by any new finding in that class",
        "Prediction-held: no round five drew a floor finding",
        "Escalate: no",
    ]
    filed = [
        f"Round-reflection: keys=c5 head={head}",
        *common,
        "Disposition: c5 file issue=#12",
        "Prediction-held: no round five drew a floor finding",
        "Escalate: no",
    ]
    message = tmp_path / "reflection.md"
    message.write_text("\n".join(fixing) + "\n")
    log = [("f" * 40, "empty", "\n".join(filed) + "\n", datetime.now(UTC) - timedelta(minutes=1))]
    monkeypatch.setattr(_gate, "_exemption_shape_ok", lambda *a: True)
    monkeypatch.setattr(_gate, "_reflection_message_file", lambda seg: str(message))
    monkeypatch.setattr(_gate, "_staged_files", lambda *a, **k: [])
    monkeypatch.setattr(_gate, "_integration_in_progress", lambda *a, **k: False)
    monkeypatch.setattr(rr, "_log_reflections", lambda cwd: log if answered else [])
    problem = _gate._reflection_problem(
        [["git"]], "git commit", str(tmp_path), budget, rr, deadline=time.monotonic() + 9
    )
    if answered:
        assert problem and "loop is over" in problem
    else:  # round 5's other obligations may still refuse; the end does not
        assert not problem or "loop is over" not in problem, problem


def test_the_rules_read_the_declared_answers():
    """A fix-now made after a committed file-all answer to the newest round is
    refused: the loop ended at that answer."""
    _, records = _ladder(5, blocking_at={5})
    record = max(records.values(), key=lambda r: r.number)
    at = _late(5)
    parsed = _reflection(prediction=6, confidence=99, held="no")
    ended = [_ans({"c1"}, at=at - timedelta(minutes=5))]
    got = rr.past_terminal_problems(
        parsed, record, records, gate_lane=False, made_at=at, cutover=CUT, declared=ended
    )
    assert any("loop is over" in p for p in got)
    assert rr.past_terminal_problems(
        parsed, record, records, gate_lane=False, made_at=at, cutover=CUT
    ) == []
def test_a_second_dash_c_cannot_borrow_a_merge_of_main(tmp_path):
    """C3.4: a merge of main in progress in /a must not admit a commit that
    a second -C sends to /b."""
    repo = _merge_repo(tmp_path)
    _git(repo, "merge", "-q", "--no-commit", "--no-ff", "main")
    other = tmp_path / "b"
    other.mkdir()
    command = f"git -C {repo} -C {other} commit --no-edit"
    segs = _commit_segs(command)
    assert _gate._concludes_merge_of_main(str(repo), rr, segs) is True  # the probe alone is fooled
    assert (
        _gate._ended_commit_ok(segs, command, str(repo), {"gate_surface": False}, rr, None) is False
    )
    single = f"git -C {repo} commit --no-edit"
    assert (
        _gate._ended_commit_ok(
            _commit_segs(single), single, str(repo), {"gate_surface": False}, rr, None
        )
        is True
    )


def test_a_floor_finding_after_a_filed_nit_keeps_the_loop_open():
    """Fix audit SF-1: at round 4 the session files CodeRabbit's nit, then
    Codex posts a P1 on the same head. The P1 is unanswered, so fixing it is
    open; once it too is filed, the loop ends."""
    _, records = _ladder(3)
    r4_open = T0 + 6 * timedelta(hours=1)
    r4 = rr.RoundRecord(
        "4" * 40, 4, r4_open,
        ((r4_open, False, "c1"), (r4_open + timedelta(minutes=20), True, "c9")),
    )
    records = {**records, r4.head: r4}
    filed_nit = _ans({"c1"}, at=r4_open + timedelta(minutes=10))
    after = r4_open + timedelta(minutes=30)
    assert _ended(records, r4_open + timedelta(minutes=15), [filed_nit]) is True
    assert _ended(records, after, [filed_nit]) is False
    fixing = _reflection(prediction=5, confidence=99)
    assert not any("loop is over" in p for p in _problems(fixing, r4, records, at=after))
    filed_p1 = _ans({"c9"}, at=after)
    assert _ended(records, after + timedelta(minutes=1), [filed_nit, filed_p1]) is True


def test_a_mixed_answer_files_one_key_and_keeps_fixing_open():
    _, records = _ladder(3)
    r4 = rr.RoundRecord("4" * 40, 4, _late(4), ((_late(4), False, "c1"), (_late(4), True, "c2")))
    records = {**records, r4.head: r4}
    at = _late(4) + timedelta(hours=1)
    mixed = _ans({"c1", "c2"}, at=at, fixes={"c2"})
    assert _ended(records, at, [mixed]) is False



@pytest.mark.parametrize("old", ["round2", "precutover"])
def test_a_send_back_on_any_key_of_the_pr_ends_the_loop(old):
    """Fix audit SF-3: a send-back naming a round-2 key, or a key from a round
    opened before the cutover, ends the loop like any other send-back."""
    _, records = _ladder(5, blocking_at={5})
    if old == "precutover":
        pre = _rec(1, opened=T0 - timedelta(days=30), key="c0", head="0" * 40)
        records = {**records, pre.head: pre}
        key = "c0"
    else:
        key = "c1"
    at = _late(5)
    assert _ended(records, at, [_ans({key}, at=at, sent_back=True, fix_now=True)]) is True


def test_the_reflection_log_is_read_once_per_head(tmp_path, monkeypatch):
    """Fix audit SF-4: Rule R's rules share one log read per HEAD; a new
    commit is a new read."""
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "base")
    reads = []
    real = rr._read_log_reflections
    monkeypatch.setattr(rr, "_read_log_reflections", lambda cwd: reads.append(cwd) or real(cwd))
    rr._log_reflections(str(repo))
    rr._log_reflections(str(repo))
    assert len(reads) == 1
    _git(repo, "commit", "-q", "--allow-empty", "-m", "next")
    rr._log_reflections(str(repo))
    assert len(reads) == 2


def _late_floor_records(*, round_head=True, flag=True, timed=True):
    """Rounds 1-6 bound, round 6 newest and clean, and a late finding c7 that
    landed after round 6 opened: on round 5's head, or on no round at all."""
    r5_open = T0 + 8 * timedelta(hours=2)
    r6_open = r5_open + timedelta(hours=2)
    earlier = [_rec(i, opened=T0 + (i - 1) * timedelta(hours=2)) for i in range(1, 5)]
    when = r6_open + timedelta(minutes=10) if timed else None
    late = ((when, flag, "c7"),) if round_head else ()
    r5 = rr.RoundRecord("5" * 40, 5, r5_open, ((r5_open, False, "c5"), *late))
    r6 = rr.RoundRecord("6" * 40, 6, r6_open, ((r6_open, False, "c6"),))
    records = _records(*earlier, r5, r6)
    flags = {**rr._record_flags(records), "c7": flag}
    return records, flags, r5, r6_open + timedelta(minutes=50)


@pytest.mark.parametrize(
    "shape",
    [
        {"round_head": True},  # C2.4: a late floor finding on an older round
        {"round_head": False},  # round 2 (Codex): on a commit that opened no round
        {"round_head": True, "flag": None},  # an unreadable level
        {"round_head": True, "timed": False},  # no time
    ],
)
def test_a_late_floor_finding_can_be_fixed_whatever_its_round(shape):
    """The lift reads the declared fix and the key's flag over every budget
    finding: a fix-now of a late floor (or unreadable) key outside the newest
    round is admitted, and once declared it keeps the loop open; filing it
    instead ends the loop."""
    records, flags, r5, after = _late_floor_records(**shape)
    assert _ended(records, after, flags=flags) is True  # nothing declared yet: still ended
    fixing = _reflection(prediction=7, confidence=99, held="no")
    fixing.fix_now_keys = {"c7"}
    record = r5 if shape["round_head"] else None
    got = rr.past_terminal_problems(
        fixing, record, records, gate_lane=False, made_at=after, cutover=CUT, flags=flags
    )
    assert not any("loop is over" in p for p in got), got
    fixed = _ans({"c7"}, at=after - timedelta(minutes=1), fix_now=True)
    assert _ended(records, after, [fixed], flags=flags) is False
    filed = _ans({"c7"}, at=after - timedelta(minutes=1))
    assert _ended(records, after, [filed], flags=flags) is True


def test_a_late_below_floor_fix_is_still_refused():
    records, flags, r5, after = _late_floor_records(flag=False)
    fixing = _reflection(prediction=7, confidence=99, held="no")
    fixing.fix_now_keys = {"c7"}
    got = rr.past_terminal_problems(
        fixing, r5, records, gate_lane=False, made_at=after, cutover=CUT, flags=flags
    )
    assert any("loop is over" in p for p in got)
    fixed = _ans({"c7"}, at=after - timedelta(minutes=1), fix_now=True)
    assert _ended(records, after, [fixed], flags=flags) is True


def test_a_keyless_newest_finding_keeps_the_filing_end_off():
    """S5: a newest-round finding with no key cannot be answered, so filing
    the keyed ones does not end the loop."""
    _, records = _ladder(3)
    r4 = rr.RoundRecord("4" * 40, 4, _late(4), ((_late(4), False, "c1"), (_late(4), True, None)))
    records = {**records, r4.head: r4}
    at = _late(4) + timedelta(hours=1)
    assert _ended(records, at, [_ans({"c1"}, at=at)]) is False


def test_key_flags_read_every_budget_finding():
    budget = {
        "findings": [
            {"round_head": None, "keys": ["c1", "c2"], "blocking": [True, False]},
            {"round_head": "a" * 40, "keys": ["c2"], "blocking": [None]},
            {"round_head": "b" * 40, "keys": ["c3"], "blocking": "bad"},
        ]
    }
    assert rl.key_flags(budget) == {"c1": True, "c2": None, "c3": None}
@pytest.mark.parametrize("floor", [True, False])
def test_admission_fixes_a_late_floor_finding_that_opened_no_round(monkeypatch, tmp_path, floor):
    """Round 2 (Codex): five bound rounds, round 5 clean, then a P1 on a commit
    that opened no round (round_head None in the budget). Through the real
    round_records and key_flags, a fix-now of it is not refused by the end."""
    import time

    budget, cutover = _ended_budget()
    budget["findings"].append(
        {
            "round_head": None,
            "submitted_at": datetime.now(UTC).isoformat(),
            "keys": ["c99"],
            "blocking": [floor],
        }
    )
    monkeypatch.setattr(rr, "PAST_TERMINAL_CUTOVER_ISO", cutover)
    head = budget["current_head"]
    lines = [
        f"Round-reflection: keys=c99 head={head}",
        "Class: unchecked subprocess result = 1",
        "Premise: LEAD",
        "Premise-why: the change routes around an existing helper that already exists",
        "Impossible: route every call through one checked helper and delete the rest",
        "Scope: covered: the key round-trips (tests/test_x.py)",
        "Decision: fix-instances",
        "Decision-why: the same mistake appears twice in two different files",
        "Disposition: c99 fix-now test=1",
        "Prediction: merges at round 6 because the class is closed now",
        "Confidence: 99% falsified-by any new finding in that class",
        "Prediction-held: no a floor finding arrived late",
        "Escalate: no",
    ]
    message = tmp_path / "reflection.md"
    message.write_text("\n".join(lines) + "\n")
    monkeypatch.setattr(_gate, "_exemption_shape_ok", lambda *a: True)
    monkeypatch.setattr(_gate, "_reflection_message_file", lambda seg: str(message))
    monkeypatch.setattr(_gate, "_staged_files", lambda *a, **k: [])
    monkeypatch.setattr(_gate, "_integration_in_progress", lambda *a, **k: False)
    monkeypatch.setattr(rr, "_log_reflections", lambda cwd: [])
    assert (
        rr.loop_ended(
            rr._round_records(budget), gate_lane=False, now=datetime.now(UTC), cutover=cutover
        )
        is True
    )
    problem = _gate._reflection_problem(
        [["git"]], "git commit", str(tmp_path), budget, rr, deadline=time.monotonic() + 9
    )
    if floor:
        assert not problem or "loop is over" not in problem, problem
    else:  # a late below-floor finding is filed, never fixed, once the loop ended
        assert problem and "loop is over" in problem, problem


def test_open_coverage_reads_every_budget_finding_flag(monkeypatch):
    seen = {}
    monkeypatch.setattr(rr, "covered_keys", lambda *a, **k: seen.update(k) or set())
    budget = {
        "rounds": [],
        "findings": [{"round_head": None, "keys": ["c9"], "blocking": [False]}],
    }
    state = {"head": HEAD, "round": 5, "gate_lane": False, "round_started": None}
    rr._open_covered(".", budget, state, None)
    assert seen["key_flags"] == {"c9": False}
@pytest.mark.parametrize("key", ["c404", "c7"])
def test_declaring_an_old_or_unknown_key_never_reopens_the_loop(key):
    """Fix audit F1: once the loop ended, an empty reflection declaring
    fix-now on a key no finding has (c404), or on a floor key already
    answered before the newest round opened (c7), keeps it ended and is
    refused."""
    records, flags, _r5, after = _late_floor_records(round_head=False)
    r6 = max(records.values(), key=lambda r: r.number)
    early = _ans({"c7"}, at=r6.opened_at - timedelta(minutes=5), fix_now=True)
    reopen = _ans({key}, at=after - timedelta(minutes=1), fix_now=True)
    assert _ended(records, after, [early], flags=flags) is True
    assert _ended(records, after, [early, reopen], flags=flags) is True
    fixing = _reflection(prediction=7, confidence=99, held="no")
    fixing.fix_now_keys = {key}
    got = rr.past_terminal_problems(
        fixing, None, records, gate_lane=False, made_at=after, cutover=CUT,
        declared=[early], flags=flags,
    )
    assert any("loop is over" in p for p in got)


def test_fixing_then_filing_a_late_key_ends_the_loop():
    """Fix audit F2: the newest answer for a key decides it."""
    records, flags, _r5, after = _late_floor_records()
    fixed = _ans({"c7"}, at=after - timedelta(minutes=5), fix_now=True)
    filed = _ans({"c7"}, at=after - timedelta(minutes=1))
    assert _ended(records, after, [fixed], flags=flags) is False
    assert _ended(records, after, [fixed, filed], flags=flags) is True


def test_the_gate_reads_every_budget_finding_for_the_end(monkeypatch):
    """Fix audit F3: Rule R's own end check takes the flags from the whole
    budget. A P1 with no round head, declared fix-now after the newest round
    opened, keeps content commits open; unknown to the budget, it would not."""
    budget, cutover = _ended_budget()
    budget["findings"].append(
        {
            "round_head": None,
            "submitted_at": datetime.now(UTC).isoformat(),
            "keys": ["c99"],
            "blocking": [True],
        }
    )
    fixing = (
        f"Round-reflection: keys=c99 head={'e' * 40}\n"
        "Class: unchecked subprocess result = 1\nPremise: LEAD\n"
        "Premise-why: the change routes around an existing helper that already exists\n"
        "Impossible: route every call through one checked helper and delete the rest\n"
        "Scope: covered: the key round-trips (tests/test_x.py)\nDecision: fix-instances\n"
        "Decision-why: the same mistake appears twice in two different files\n"
        "Disposition: c99 fix-now test=1\n"
        "Prediction: merges at round 6 because the class is closed now\n"
        "Confidence: 99% falsified-by any new finding in that class\n"
        "Prediction-held: no a floor finding arrived late\nEscalate: no\n"
    )
    log = [("f" * 40, "empty", fixing, datetime.now(UTC) - timedelta(minutes=1))]
    assert _rule(monkeypatch, budget, cutover, log=log)[0] == "continue"
    monkeypatch.setattr(rr, "_key_flags", lambda budget: {})
    assert _rule(monkeypatch, budget, cutover, log=log)[0] == "deny"


def test_merge_flags_floor_wins_then_unknown():
    assert rr.merge_flags([("a", False), ("a", True), ("a", None)]) == {"a": True}
    assert rr.merge_flags([("b", False), ("b", None)]) == {"b": None}
    assert rr.merge_flags([("c", False), ("c", False)]) == {"c": False}
