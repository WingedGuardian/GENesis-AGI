"""The past-round-4 reflection rules (#4133, rework PR 2 of #4368): P, H, F and
E, read from the budget alone, with the rounds a fix did not produce left out.
One test per row of the plan's writer-state table."""

from __future__ import annotations

import itertools
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import review_budget as rb  # noqa: E402
import review_findings as rf  # noqa: E402
import review_past_terminal as pt  # noqa: E402
import review_reflection as rr  # noqa: E402

CUT = "2026-11-01T00:00:00+00:00"
T0 = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
HEAD = "a" * 40
CODEX = rf.CODEX_LOGIN
P1 = "**<sub><sub>![P1 Badge](https://img.shields.io/badge/P1-orange)</sub></sub>  Bug**"
P2 = "**<sub><sub>![P2 Badge](https://img.shields.io/badge/P2-yellow)</sub></sub>  Nit**"


def _rec(number, *, floor=False, opened=None, at=None, key=None, live=True, adv=True, head=None):
    opened = T0 + (number - 1) * timedelta(hours=2) if opened is None else opened
    at = opened if at is None else at
    key = key or f"c{number}"
    return pt.RoundRecord(
        head or f"{number:040x}",
        number,
        opened,
        ((at, floor, key),),
        live=live,
        advanced_by_fix=adv,
    )


def _records(*recs):
    return {r.head: r for r in recs}


def _ladder(n, *, floor_at=(), **last):
    recs = [_rec(i, floor=i in floor_at) for i in range(1, n)]
    last.setdefault("floor", n in floor_at)
    recs.append(_rec(n, **last))
    return recs[-1], _records(*recs)


def _after(n, hours=1):
    """A time inside the PR's life after round n opened (settled after 30 min)."""
    return T0 + (n - 1) * timedelta(hours=2) + timedelta(hours=hours)


def _reflection(
    keys=("c5",),
    *,
    decision="fix-instances",
    how="fix-now test=1",
    prediction=None,
    confidence=None,
    held=None,
):
    lines = [
        f"Round-reflection: keys={','.join(keys)} head={HEAD}",
        "Class: unchecked subprocess result = 1",
        "Premise: LEAD",
        "Premise-why: the change routes around an existing helper that already exists",
        "Impossible: route every call through one checked helper and delete the rest",
        "Scope: covered: the key round-trips (tests/test_x.py)",
        f"Decision: {decision}",
        "Decision-why: the same mistake appears twice in two different files",
    ]
    lines += [f"Disposition: {k} {how}" for k in keys]
    if prediction is not None:
        lines.append(f"Prediction: merges at round {prediction} because the class is closed now")
    if confidence is not None:
        lines.append(f"Confidence: {confidence}% falsified-by any new finding in that class")
    if held is not None:
        lines.append(f"Prediction-held: {held} the last round drew only minor findings")
    lines.append("Escalate: no")
    got = rr.parse("\n".join(lines) + "\n")
    assert got.ok, got.problems
    return got


def _problems(parsed, record, records, *, at, gate=False, cutover=CUT):
    return pt.problems(parsed, record, records, gate_lane=gate, made_at=at, cutover=cutover)


# -- the grammar --------------------------------------------------------------


def test_the_three_lines_parse_and_are_optional():
    got = _reflection(prediction=5, confidence=97, held="no")
    assert (got.prediction_round, got.confidence, got.held) == (5, 97, "no")
    plain = _reflection()
    assert (plain.prediction_round, plain.confidence, plain.held) == (None, None, None)
    bad = rr.parse(
        "\n".join([f"Round-reflection: keys=c1 head={HEAD}", "Confidence: 101% falsified-by x"])
    )
    assert any("Confidence" in p for p in bad.problems)


# -- off: nothing binds -------------------------------------------------------


def test_nothing_binds_before_round_four_on_the_gate_lane_or_before_the_cutover():
    record, records = _ladder(3)
    assert _problems(_reflection(["c3"]), record, records, at=_after(3)) == []
    record, records = _ladder(6)
    assert _problems(_reflection(["c6"]), record, records, at=_after(6), gate=True) == []
    old = T0 - timedelta(days=30)
    recs = [_rec(i, opened=old + i * timedelta(hours=1)) for i in range(1, 7)]
    assert _problems(_reflection(["c6"]), recs[-1], _records(*recs), at=_after(6)) == []


def test_the_shipped_cutover_binds_nothing():
    """PR 2 ships the rules off; PR 3 sets the date (owner 2fd36db6)."""
    assert pt.PAST_TERMINAL_CUTOVER_ISO is None
    record, records = _ladder(6)
    assert (
        pt.problems(_reflection(["c6"]), record, records, gate_lane=False, made_at=_after(6)) == []
    )


def test_unplaceable_rounds_or_an_unreadable_time_bind_nothing():
    """N3: never a refusal of every reflection, send-backs included."""
    record, records = _ladder(6)
    assert _problems(_reflection(["c6"]), record, None, at=_after(6)) == []
    assert _problems(_reflection(["c6"]), record, records, at=None) == []


# -- P -----------------------------------------------------------------------


def test_round_four_fixing_needs_the_next_round_and_high_confidence():
    record, records = _ladder(4)
    at = _after(4)
    keys = ["c4"]
    assert _problems(_reflection(keys, prediction=5, confidence=95), record, records, at=at) == []
    for bad in (
        _reflection(keys),
        _reflection(keys, prediction=6, confidence=99),
        _reflection(keys, prediction=5, confidence=94),
    ):
        assert _problems(bad, record, records, at=at), bad


def test_filing_or_no_defect_or_sending_back_owes_no_prediction():
    record, records = _ladder(4)
    at = _after(4)
    for how in ("file issue=#12", "no-defect reply=77"):
        assert _problems(_reflection(["c4"], how=how), record, records, at=at) == []
    sent = _reflection(["c4"], decision="send-back", how="file issue=#12")
    assert _problems(sent, record, records, at=at) == []


def test_the_prediction_is_owed_for_a_key_of_an_older_round_too():
    """P is PR-wide: a fix-now of a round-3 key at round 4 still owes it."""
    recs = [_rec(i) for i in range(1, 5)]
    records = _records(*recs)
    assert _problems(_reflection(["c3"]), recs[2], records, at=_after(4))


# -- H -----------------------------------------------------------------------


def test_round_five_needs_the_held_line():
    record, records = _ladder(5, floor_at={5})
    fixing = _reflection(prediction=6, confidence=99)
    assert any("Prediction-held" in p for p in _problems(fixing, record, records, at=_after(5)))


def test_yes_is_refused_on_a_floor_round_an_unreadable_level_or_before_settling():
    floor, floor_records = _ladder(5, floor_at={5})
    yes = _reflection(how="file issue=#12", held="yes")
    assert any("is wrong" in p for p in _problems(yes, floor, floor_records, at=_after(5)))
    unread, unread_records = _ladder(5, floor=None)
    assert any(
        "cannot be verified" in p for p in _problems(yes, unread, unread_records, at=_after(5))
    )
    clean, clean_records = _ladder(5)
    early = _after(5, hours=0.25)
    assert any("settled" in p for p in _problems(yes, clean, clean_records, at=early))
    assert _problems(yes, clean, clean_records, at=_after(5)) == []


def test_a_floor_round_may_fix_and_must_say_no():
    record, records = _ladder(5, floor_at={5})
    good = _reflection(prediction=6, confidence=99, held="no")
    assert _problems(good, record, records, at=_after(5)) == []


def test_a_finding_after_the_reflection_never_changes_its_answer():
    """An early nit settles the round; a P1 landing after the reflection does
    not make its earlier ``yes`` wrong."""
    early = _rec(5)
    record = pt.RoundRecord(
        early.head,
        5,
        early.opened_at,
        (*early.findings, (_after(5, hours=3), True, "c50")),
    )
    records = _records(*[_rec(i) for i in range(1, 5)], record)
    yes = _reflection(how="file issue=#12", held="yes")
    assert _problems(yes, record, records, at=_after(5)) == []


# -- F -----------------------------------------------------------------------


def test_two_failed_predictions_leave_only_send_back():
    record, records = _ladder(6, floor_at={5, 6})
    fixing = _reflection(["c6"], prediction=7, confidence=99, held="no")
    assert any("send-back" in p for p in _problems(fixing, record, records, at=_after(6)))
    filing = _reflection(["c6"], how="file issue=#12", held="no")
    assert any("send-back" in p for p in _problems(filing, record, records, at=_after(6)))
    sent = _reflection(["c6"], decision="send-back", how="file issue=#12", held="no")
    assert _problems(sent, record, records, at=_after(6)) == []


def test_a_slow_floor_finding_still_counts_as_a_failure():
    """Audit N1: no settle cap. A P1 35 minutes after each round opened."""
    recs = [_rec(i) for i in range(1, 5)] + [
        _rec(i, floor=True, at=_after(i, hours=35 / 60)) for i in (5, 6, 7)
    ]
    assert pt.prediction_failures(_records(*recs), _after(7, hours=3), CUT) == 3


def test_a_round_a_fix_did_not_produce_is_no_failure():
    """Audit S1 scenario B: two floor rounds with only a merge of main between."""
    recs = [_rec(i) for i in range(1, 5)] + [
        _rec(5, floor=True),
        _rec(6, floor=True, adv=False),
    ]
    assert pt.prediction_failures(_records(*recs), _after(6), CUT) == 1
    dropped = [_rec(i) for i in range(1, 5)] + [
        _rec(5, floor=True),
        _rec(6, floor=True, live=False),
    ]
    assert pt.prediction_failures(_records(*dropped), _after(6), CUT) == 1


def test_acknowledge_is_bound_by_the_two_failure_rule():
    record, records = _ladder(6, floor_at={5, 6})
    ack = _reflection(["c6"], decision="acknowledge", how="no-defect reply=77", held="no")
    assert any("send-back" in p for p in _problems(ack, record, records, at=_after(6)))


# -- E -----------------------------------------------------------------------


def test_a_round_five_with_no_floor_finding_takes_no_fix_now_for_its_keys():
    record, records = _ladder(5)
    fixing = _reflection(prediction=6, confidence=99, held="no")
    assert any("fixing ends here" in p for p in _problems(fixing, record, records, at=_after(5)))
    assert (
        _problems(_reflection(how="file issue=#12", held="no"), record, records, at=_after(5)) == []
    )


def test_end_of_fixing_is_per_key_an_older_floor_key_stays_fixable():
    """Round 5 drew a floor finding (c5), round 6 none (c6): c5 may still be
    fixed under its own round; c6 may not."""
    r5 = _rec(5, floor=True)
    r6 = _rec(6)
    records = _records(*[_rec(i) for i in range(1, 5)], r5, r6)
    old = _reflection(["c5"], prediction=7, confidence=99, held="no")
    assert _problems(old, r5, records, at=_after(6)) == []
    new = _reflection(["c6"], prediction=7, confidence=99, held="no")
    assert any("fixing ends here" in p for p in _problems(new, r6, records, at=_after(6)))


def test_a_round_a_merge_produced_never_ends_fixing():
    """Spec acceptance 4: round 6 reached by a merge of main draws a nit; the
    round-5 P1 and round 6's own nit both stay fixable with the P lines."""
    r5 = _rec(5, floor=True)
    r6 = _rec(6, adv=False)
    records = _records(*[_rec(i) for i in range(1, 5)], r5, r6)
    for key, record in (("c5", r5), ("c6", r6)):
        fixing = _reflection([key], prediction=7, confidence=99, held="no")
        assert _problems(fixing, record, records, at=_after(6)) == [], key


def test_a_round_on_a_dropped_head_is_never_newest():
    """Audit S2: a slow review of a force-pushed-away commit opened round 6."""
    r5 = _rec(5, floor=True)
    r6 = _rec(6, live=False)
    records = _records(*[_rec(i) for i in range(1, 5)], r5, r6)
    assert pt.newest_under_rules(records, _after(6)) is r5
    fixing = _reflection(["c5"], prediction=7, confidence=99, held="no")
    assert _problems(fixing, r5, records, at=_after(6)) == []


def test_an_unknown_fix_turns_the_rules_off_instead_of_using_an_older_round():
    """Architect S1: never fall back past an unknown newest round."""
    r5 = _rec(5)
    r6 = _rec(6, floor=True, adv=None)
    records = _records(*[_rec(i) for i in range(1, 5)], r5, r6)
    assert pt.newest_under_rules(records, _after(6)) is pt.UNKNOWN
    assert _problems(_reflection(["c5"]), r5, records, at=_after(6)) == []


# -- send-back ----------------------------------------------------------------


def test_past_round_three_a_send_back_fixes_nothing_now():
    record, records = _ladder(4)
    sent = _reflection(["c4"], decision="send-back", prediction=5, confidence=99)
    assert any(
        "send-back fixes nothing" in p for p in _problems(sent, record, records, at=_after(4))
    )


def _states():
    """Every combination of the facts the rules read, for a PR of 4-7 rounds."""
    for n, floors, newest_adv, live, level in itertools.product(
        (4, 5, 6, 7),
        ((), (5,), (5, 6), (5, 6, 7)),
        (True, False, None),
        (True, False),
        (True, False, None),
    ):
        recs = [_rec(i, floor=(i in floors) if i < n else level) for i in range(1, n)]
        recs.append(_rec(n, floor=level, adv=newest_adv, live=live))
        yield recs


@pytest.mark.parametrize("how", ["file issue=#12", "no-defect reply=77"])
def test_send_back_filing_everything_with_no_passes_in_every_state(how):
    sent_count = 0
    for recs in _states():
        records = _records(*recs)
        for record, at in itertools.product(
            [*recs, None], (_after(len(recs)), _after(len(recs), 0.1))
        ):
            key = f"c{record.number}" if record else "c99"
            sent = _reflection([key], decision="send-back", how=how, held="no")
            assert _problems(sent, record, records, at=at) == [], (record, at)
            sent_count += 1
    assert sent_count > 1000


# -- round records from a real budget ------------------------------------------


H = [f"{i:040x}" for i in range(1, 8)]
OUTSIDE = "f" * 40
TREE = [f"{i:040x}"[::-1] for i in range(1, 8)]


def _rv(head, when, rid, cid, badge=P2):
    return {
        "login": CODEX,
        "commit_id": head,
        "state": "COMMENTED",
        "submitted_at": when.isoformat().replace("+00:00", "Z"),
        "body": "",
        "top_level": [badge],
        "id": rid,
        "top_level_ids": [cid],
    }


def _budget(commits, reviews, head):
    return rb.evaluate_evidence(
        current_head=head,
        commit_heads=commits,
        reviews=reviews,
        issue_comments=(),
        changed_files=("src/x.py",),
        cutover="2026-06-01T00:00:00+00:00",
        external_identity_templates=(),
    )


def _row(i, parents, tree):
    return {"sha": H[i], "parents": parents, "tree": tree, "committed_at": None}


def test_round_records_mark_a_round_a_merge_of_main_produced():
    """H0 reviewed, H1 a fix (tree changes), H2 a merge of main (second parent
    outside the PR), H3 an empty commit: rounds on H2 and H3 are not produced
    by a fix; H1's is."""
    commits = [
        _row(0, [], TREE[0]),
        _row(1, [H[0]], TREE[1]),
        _row(2, [H[1], OUTSIDE], TREE[2]),
        _row(3, [H[2]], TREE[2]),
    ]
    reviews = [_rv(H[i], T0 + timedelta(hours=i), 10 + i, 100 + i) for i in range(4)]
    budget = _budget(commits, reviews, H[3])
    assert budget["status"] == "ok", budget
    records = pt.round_records(budget)
    assert {r.head: (r.number, r.live, r.advanced_by_fix) for r in records.values()} == {
        H[0]: (1, True, True),
        H[1]: (2, True, True),
        H[2]: (3, True, False),
        H[3]: (4, True, False),
    }
    assert records[H[1]].findings[0][1] is False  # a P2: below the floor


def test_round_records_read_the_level_as_written():
    commits = [_row(0, [], TREE[0]), _row(1, [H[0]], TREE[1])]
    reviews = [_rv(H[0], T0, 10, 100), _rv(H[1], T0 + timedelta(hours=1), 11, 111, badge=P1)]
    records = pt.round_records(_budget(commits, reviews, H[1]))
    assert records[H[1]].floor(T0 + timedelta(hours=2)) == (True, ["c111"])


def test_an_unknown_tree_makes_the_fix_unknown_and_any_proof_wins():
    commits = [
        _row(0, [], TREE[0]),
        _row(1, [H[0]], None),
        _row(2, [H[1]], TREE[2]),
    ]
    reviews = [_rv(H[0], T0, 10, 100), _rv(H[2], T0 + timedelta(hours=1), 12, 120)]
    records = pt.round_records(_budget(commits, reviews, H[2]))
    assert records[H[2]].advanced_by_fix is None
    proved = [_row(0, [], TREE[0]), _row(1, [H[0]], TREE[1]), _row(2, [H[1]], None)]
    records = pt.round_records(_budget(proved, reviews, H[2]))
    assert records[H[2]].advanced_by_fix is True


def test_a_merge_between_two_pr_lines_that_changes_the_tree_is_a_fix():
    commits = [
        _row(0, [], TREE[0]),
        _row(1, [H[0]], TREE[1]),
        _row(2, [H[0]], TREE[2]),
        _row(3, [H[1], H[2]], TREE[3]),
    ]
    reviews = [_rv(H[1], T0, 10, 100), _rv(H[3], T0 + timedelta(hours=1), 13, 130)]
    records = pt.round_records(_budget(commits, reviews, H[3]))
    assert records[H[3]].advanced_by_fix is True


def test_the_held_reader_and_the_round_reader_disagree_on_a_conflicted_main_merge():
    """N5, documented: ``fix_rows`` may count a conflict-resolving merge of main
    as a fix (its tree is not git's merge result); ``advanced_by_fix`` never
    does, so such a merge never produces a round the rules read."""
    commits = [_row(0, [], TREE[0]), _row(1, [H[0], OUTSIDE], TREE[1])]
    reviews = [_rv(H[0], T0, 10, 100), _rv(H[1], T0 + timedelta(hours=1), 11, 110)]
    records = pt.round_records(_budget(commits, reviews, H[1]))
    assert records[H[1]].advanced_by_fix is False


def test_a_dropped_head_is_not_live():
    gone = "e" * 40
    commits = [_row(0, [], TREE[0]), _row(1, [H[0]], TREE[1])]
    reviews = [
        _rv(H[0], T0, 10, 100),
        _rv(gone, T0 + timedelta(hours=1), 11, 110),
        _rv(H[1], T0 + timedelta(hours=2), 12, 120),
    ]
    records = pt.round_records(_budget(commits, reviews, H[1]))
    assert records[gone].live is False and records[gone].advanced_by_fix is False
    assert records[H[1]].advanced_by_fix is True


# -- the callers -------------------------------------------------------------


@pytest.fixture
def placed(monkeypatch):
    """Rounds 1-6 bound by the rules: round 5 drew a P1 (c5), round 6 none (c6)."""
    recs = [_rec(i) for i in range(1, 5)] + [_rec(5, floor=True), _rec(6)]
    records = _records(*recs)
    monkeypatch.setattr(pt, "PAST_TERMINAL_CUTOVER_ISO", CUT)
    monkeypatch.setattr(pt, "round_records", lambda budget: records)
    return recs, records


def test_admission_judges_every_named_key_under_its_own_round_head(placed, monkeypatch):
    """Architect S3: a reflection fixing c5 (fixable) and c6 (round 6 ended)
    is refused, not admitted on c5; each key's round is found by its head."""
    import review_enforcement_commit as rec

    recs, _ = placed
    monkeypatch.setattr(rec, "datetime", _FixedNow)
    both = _reflection(["c5", "c6"], prediction=7, confidence=99, held="no")
    rounds = {"c5": [(5, recs[4].head)], "c6": [(6, recs[5].head)]}
    found = rec._past_terminal_problem(both, rounds, {}, False)
    assert any("fixing ends here" in p for p in found), found
    only_old = _reflection(["c5"], prediction=7, confidence=99, held="no")
    assert rec._past_terminal_problem(only_old, {"c5": [(5, recs[4].head)]}, {}, False) == []
    # Round 0 (a late key with no round): the PR-wide rules only.
    bare = _reflection(["c9"])
    assert rec._past_terminal_problem(bare, {"c9": [(None, None)]}, {}, False)


def test_admission_never_looks_a_round_up_by_number(placed, monkeypatch):
    """Audit S2: the round number and the round head disagree when a dropped
    head opened a later round; the head decides."""
    import review_enforcement_commit as rec

    recs, _ = placed
    monkeypatch.setattr(rec, "datetime", _FixedNow)
    fixing = _reflection(["c5"], prediction=7, confidence=99, held="no")
    assert rec._past_terminal_problem(fixing, {"c5": [(6, recs[4].head)]}, {}, False) == []


class _FixedNow(datetime):
    @classmethod
    def now(cls, tz=None):
        return _after(6)


def test_the_held_messages_name_an_unreadable_settle_time_and_an_unkeyed_finding():
    untimed = pt.RoundRecord(f"{5:040x}", 5, T0 + timedelta(hours=8), ((None, False, "c5"),))
    records = _records(*[_rec(i) for i in range(1, 5)], untimed)
    yes = _reflection(how="file issue=#12", held="yes")
    found = _problems(yes, untimed, records, at=_after(5))
    assert any("settle time is unreadable" in p for p in found) and not any("?" in p for p in found)
    keyless = pt.RoundRecord(
        f"{5:040x}", 5, T0 + timedelta(hours=8), ((T0 + timedelta(hours=8), True, None),)
    )
    assert keyless.floor(_after(5)) == (True, ["an unkeyed finding"])
