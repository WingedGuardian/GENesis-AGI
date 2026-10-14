"""Late findings in the round reflection: owed from the cutover on, and each
credited only under its own round's context.

The open round is never judged here (``covered_keys`` keeps it), so with the
cutover None everything owed is exactly what it was. Coverage tests run in
real temporary git repositories with pinned commit dates, because emptiness,
ancestry and the fix-time rule are git behaviours.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "scripts"))

import review_reflection as rr  # noqa: E402

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
CUT = (T0 - timedelta(days=1)).isoformat()


def _iso(minutes: int) -> str:
    return (T0 + timedelta(minutes=minutes)).isoformat()


def _block(keys, head, *, extra=()):
    lines = [
        f"Round-reflection: keys={','.join(keys)} head={head}",
        "Class: unchecked subprocess result = 1",
        "Premise: LEAD",
        "Premise-why: the change routes around an existing helper that already exists",
        "Impossible: route every call through one checked helper and delete the rest",
        "Scope: covered: the key round-trips (tests/test_x.py)",
        "Decision: close-class",
        "Decision-why: the same mistake appears twice in two different files",
        *[f"Disposition: {k} fix-now test=1" for k in keys],
        *extra,
        "Escalate: no",
    ]
    return "\n".join(lines) + "\n"


# -- pure: what is owed --------------------------------------------------------


def _budget(**over):
    budget = {
        "status": "ok",
        "count": 1,
        "round_state": "complete",
        "current_head": "f" * 40,
        "gate_surface": False,
        "reflection_keys": "ok",
        "open_keys": [],
        "rounds": [{"head": "a" * 40, "reviews": [{"submitted_at": _iso(0)}]}],
        "findings": [
            {"head": "a" * 40, "submitted_at": _iso(0), "keys": ["c1"], "round_head": "a" * 40},
            {"head": "a" * 40, "submitted_at": _iso(90), "keys": ["c9"], "round_head": "a" * 40},
        ],
        "expected_reviewers": [],
        "reviewers_reported": [],
        "commits": [
            {"sha": "a" * 40, "parents": []},
            {"sha": "e" * 40, "parents": []},
            {"sha": "f" * 40, "parents": ["a" * 40]},
        ],
    }
    budget.update(over)
    return budget


@pytest.mark.parametrize(
    "over",
    [
        {},
        {"round_state": "open", "open_keys": ["c1"]},
        {"round_state": "open", "open_keys": ["c1"], "reflection_keys": "unknown"},
        {"findings": [{"head": "a" * 40, "submitted_at": "garbage", "keys": [None]}]},
        {"status": "unknown"},
    ],
)
def test_with_no_cutover_owed_is_exactly_the_open_rounds(over):
    budget = _budget(**over)
    late = rr.owed_state(budget, [], now=T0, cutover=None)
    without = {k: v for k, v in late.items() if k not in ("cutover", "late_keys")}
    baseline = rr.owed_state(budget, [], now=T0)
    assert late["owed"] == baseline["owed"]
    assert without == {k: v for k, v in baseline.items() if k not in ("cutover", "late_keys")}
    assert late["settled"] == baseline["settled"]


def test_nothing_late_is_owed_until_a_cutover_is_set():
    # The shipped cutover is None (ruling 50cc9dec): B1b-1b sets it.
    assert rr.REFLECTION_CUTOVER_ISO is None
    assert rr.late_findings(_budget(), None) == []
    state = rr.owed_state(_budget(), [], now=T0)
    assert state["owed"] == [] and state["late_keys"] == [] and state["settled"] is False


def test_findings_from_the_cutover_on_are_owed_after_the_round_completes():
    state = rr.owed_state(_budget(), [], now=T0, cutover=CUT)
    assert state["owed"] == ["c1", "c9"]
    # Those reviews have all landed: a finished round's evidence is final,
    # whether or not anything is owed, and unknown is never settled.
    assert state["settled"] is True
    done = rr.owed_state(_budget(), [], now=T0, cutover=CUT, late_covered=["c1", "c9"])
    assert done["owed"] == [] and done["settled"] is True
    unknown = rr.owed_state(_budget(), [], now=T0, cutover="soon")
    assert unknown["owed"] is None and unknown["settled"] is False
    assert rr.owed_state(_budget(), [], now=T0, cutover=CUT, late_covered=["c9"])["owed"] == ["c1"]


def test_open_round_coverage_never_clears_a_late_key():
    """Codex c4266451771: a reflection judged under the OPEN round that also
    names an older round's late key must not clear it; only a credit made
    under that key's own round does."""
    budget = _budget(round_state="open", open_keys=["c2"])
    state = rr.owed_state(budget, ["c2", "c9"], now=T0, cutover=CUT)
    assert state["owed"] == ["c1", "c9"]
    state = rr.owed_state(budget, ["c2"], now=T0, cutover=CUT, late_covered=["c9"])
    assert state["owed"] == ["c1"]


def test_findings_before_the_cutover_are_not_owed():
    state = rr.owed_state(_budget(), [], now=T0, cutover=_iso(30))
    assert state["owed"] == ["c9"]


def test_the_open_rounds_own_keys_are_not_counted_twice():
    budget = _budget(round_state="open", open_keys=["c1"])
    assert rr.owed_state(budget, [], now=T0, cutover=CUT)["owed"] == ["c1", "c9"]


@pytest.mark.parametrize(
    "finding",
    [
        {"head": "a" * 40, "submitted_at": _iso(5), "keys": [None]},
        {"head": "a" * 40, "submitted_at": "not a time", "keys": ["c3"]},
    ],
)
def test_an_unreadable_finding_from_the_cutover_on_makes_owed_unknown(finding):
    budget = _budget(findings=[finding])
    assert rr.owed_state(budget, [], now=T0, cutover=CUT)["owed"] is None
    open_budget = _budget(findings=[finding], round_state="open", open_keys=["c1"])
    assert rr.owed_state(open_budget, [], now=T0, cutover=CUT)["owed"] is None


def test_an_unkeyable_finding_before_the_cutover_changes_nothing():
    old = {"head": "a" * 40, "submitted_at": _iso(-60 * 48), "keys": [None]}
    budget = _budget(findings=[old, *_budget()["findings"]])
    assert rr.owed_state(budget, [], now=T0, cutover=CUT)["owed"] == ["c1", "c9"]


def test_an_unreadable_cutover_is_unknown():
    assert rr.owed_state(_budget(), [], now=T0, cutover="soon")["owed"] is None


# -- pure: a round's context ----------------------------------------------------


def test_rounds_are_numbered_by_when_they_opened_not_by_list_order():
    a, b = "a" * 40, "b" * 40
    budget = _budget(
        rounds=[
            {"head": b, "reviews": [{"submitted_at": _iso(60)}]},
            {"head": a, "reviews": [{"submitted_at": _iso(0)}]},
        ]
    )
    later = T0 + timedelta(hours=2)
    assert rr.round_context(budget, a, later) == rr.RoundContext(1, _iso(0), ())
    assert rr.round_context(budget, b, later) == rr.RoundContext(2, _iso(60), (a,))
    # As of a time before round b opened, b is no round yet.
    assert rr.round_context(budget, b, T0 + timedelta(minutes=30)) is None
    assert rr.round_context(budget, "c" * 40, later) is None


def test_legacy_rounds_are_ordered_by_their_marks_never_by_sha():
    """Codex c4266451765: legacy rounds carry no review times. Their order
    comes from when their heads were reviewed (``marks``); the SHA order here
    is the opposite one."""
    a, b, new = "a" * 40, "b" * 40, "0" * 40
    budget = _budget(
        rounds=[
            {"head": a, "reviews": []},
            {"head": b, "reviews": []},
            {"head": new, "reviews": [{"submitted_at": _iso(30)}]},
        ],
        marks=[
            {"head": a, "at": _iso(20)},
            {"head": b, "at": _iso(10)},
            {"head": b, "at": _iso(50)},
        ],
    )
    later = T0 + timedelta(hours=1)
    assert rr.round_context(budget, b, later) == rr.RoundContext(1, _iso(10), ())
    assert rr.round_context(budget, a, later) == rr.RoundContext(2, _iso(20), (b,))
    assert rr.round_context(budget, new, later) == rr.RoundContext(3, _iso(30), (b, a))


def test_a_round_with_no_known_time_makes_late_debt_unknown():
    legacy = "e" * 40
    budget = _budget(
        rounds=[{"head": legacy, "reviews": []}],
        findings=[{"head": legacy, "submitted_at": _iso(20), "keys": ["c4"], "round_head": legacy}],
    )
    assert rr.round_context(budget, legacy, T0 + timedelta(hours=1)) is None
    assert rr.owed_state(budget, [], now=T0, cutover=CUT)["owed"] is None
    # No cutover: nothing reads the order, so nothing changes.
    assert rr.owed_state(budget, [], now=T0)["owed"] == []


def test_a_key_with_no_placeable_round_makes_owed_unknown():
    """A late review with no round head whose ancestry is unknown could never
    be answered: owed is unknown, not a key owed for ever."""
    budget = _budget(
        commits=[{"sha": "d" * 40, "parents": None}],
        findings=[{"head": "d" * 40, "submitted_at": _iso(5), "keys": ["c7"], "round_head": None}],
    )
    assert rr.owed_state(budget, [], now=T0, cutover=CUT)["owed"] is None


# -- coverage, in a real repository ------------------------------------------------


def _git(repo: Path, *args: str, when: datetime | None = None) -> str:
    env = dict(os.environ)
    if when is not None:
        stamp = when.strftime("%Y-%m-%dT%H:%M:%S+0000")
        env.update(GIT_AUTHOR_DATE=stamp, GIT_COMMITTER_DATE=stamp)
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True, env=env
    ).stdout.strip()


def _change(repo: Path, content: str, when: datetime) -> str:
    (repo / "f.txt").write_text(content)
    _git(repo, "commit", "-q", "-am", f"set {content.strip()}", when=when)
    return _git(repo, "rev-parse", "HEAD")


def _reflect(repo: Path, tmp_path: Path, text: str, when: datetime) -> str:
    message = tmp_path / "message.md"
    message.write_text(text)
    _git(repo, "commit", "-q", "--allow-empty", "--cleanup=verbatim", "-F", str(message), when=when)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def pr(tmp_path):
    """A base commit, then round head H (reviewed at T0) and its fix F (T0+60m)."""
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.name", "t")
    _git(path, "config", "user.email", "t@example.com")
    (path / "f.txt").write_text("x\n")
    _git(path, "add", "f.txt")
    _git(path, "commit", "-q", "-m", "base", when=T0 - timedelta(days=2))
    base = _git(path, "rev-parse", "HEAD")
    _git(path, "checkout", "-q", "-b", "feat/x")
    head = _change(path, "y\n", T0 - timedelta(minutes=10))
    fix = _change(path, "z\n", T0 + timedelta(minutes=60))
    return path, base, head, fix


def _pr_budget(base, head, fix, *, findings, gate=False, current=None, parents=None):
    return {
        "status": "ok",
        "count": 1,
        "round_state": "complete",
        "current_head": current or fix,
        "gate_surface": gate,
        "reflection_keys": "ok",
        "open_keys": [],
        "rounds": [{"head": head, "reviews": [{"submitted_at": _iso(0)}]}],
        "commits": [
            {"sha": head, "parents": [base]},
            {"sha": fix, "parents": parents if parents is not None else [head]},
        ],
        "findings": findings,
    }


def _finding(key, head, minutes, round_head):
    return {"head": head, "submitted_at": _iso(minutes), "keys": [key], "round_head": round_head}


def _credit(path, budget, *, gate=False):
    late = rr.late_findings(budget, CUT)
    return rr.covered_late_keys(str(path), budget, late, gate_lane=gate, acceptance=None)


def test_a_review_after_the_fix_is_answered_at_the_fix(pr, tmp_path):
    """The owner's scenario: a review of round head H lands after the fix F
    exists; a reflection at F, after the review, answers it under H's round."""
    path, base, head, fix = pr
    budget = _pr_budget(base, head, fix, findings=[_finding("c9", head, 90, head)])
    _reflect(path, tmp_path, _block(["c9"], fix), T0 + timedelta(minutes=120))
    assert _credit(path, budget) == {"c9"}


def test_a_reflection_made_after_the_fix_cannot_clear_a_review_from_before_it(pr, tmp_path):
    """c1 arrived before the fix: it was owed before the fix, so only a
    reflection before the fix answers it. Clearing it afterwards is a
    rationalisation."""
    path, base, head, fix = pr
    budget = _pr_budget(base, head, fix, findings=[_finding("c1", head, 0, head)])
    _reflect(path, tmp_path, _block(["c1"], fix), T0 + timedelta(minutes=120))
    assert _credit(path, budget) == set()


def test_an_answer_cannot_predate_its_finding(pr, tmp_path):
    path, base, head, fix = pr
    budget = _pr_budget(base, head, fix, findings=[_finding("c9", head, 180, head)])
    _reflect(path, tmp_path, _block(["c9"], fix), T0 + timedelta(minutes=120))
    assert _credit(path, budget) == set()


def test_the_answer_is_held_to_its_own_rounds_obligations(pr, tmp_path):
    """Gate lane, round 1 owes an audit: a reflection without one answers
    nothing, and the same reflection citing the round's audit does."""
    path, base, head, fix = pr
    budget = _pr_budget(base, head, fix, findings=[_finding("c9", head, 90, head)], gate=True)
    _reflect(path, tmp_path, _block(["c9"], fix), T0 + timedelta(minutes=120))
    assert _credit(path, budget, gate=True) == set()
    audit = tmp_path / "audit.txt"
    audit.write_text(
        "Severity ladder: BLOCKER none; SHOULD-FIX scripts/x.py:12 the loop is unbounded; "
        "NOTE scripts/y.py:3 a stale comment. Scope: the whole diff was read. " * 4
    )
    stamp = (T0 + timedelta(minutes=100)).timestamp()
    os.utime(audit, (stamp, stamp))
    _reflect(
        path,
        tmp_path,
        _block(["c9"], fix, extra=[f"Audit-evidence: {audit} class-audit"]),
        T0 + timedelta(minutes=130),
    )
    assert _credit(path, budget, gate=True) == {"c9"}


def test_a_late_review_with_no_earlier_round_owes_no_evidence(pr, tmp_path):
    """Round 0 (owner, rev 3): even the gate lane owes only the grammar."""
    path, base, head, fix = pr
    budget = _pr_budget(base, head, fix, findings=[_finding("c9", head, 90, None)], gate=True)
    budget["rounds"] = []
    _reflect(path, tmp_path, _block(["c9"], fix), T0 + timedelta(minutes=120))
    assert _credit(path, budget, gate=True) == {"c9"}


def test_unknown_ancestry_credits_nothing(pr, tmp_path):
    path, base, head, fix = pr
    budget = _pr_budget(base, head, fix, findings=[_finding("c9", head, 90, head)])
    budget["commits"][1]["parents"] = None
    _reflect(path, tmp_path, _block(["c9"], fix), T0 + timedelta(minutes=120))
    assert _credit(path, budget) == set()


def test_a_reflection_off_the_current_line_credits_nothing(pr, tmp_path):
    path, base, head, fix = pr
    other = "9" * 40
    budget = _pr_budget(base, head, fix, findings=[_finding("c9", head, 90, head)], current=other)
    budget["commits"].append({"sha": other, "parents": [head]})
    _reflect(path, tmp_path, _block(["c9"], fix), T0 + timedelta(minutes=120))
    assert _credit(path, budget) == set()


def test_a_reflection_with_content_or_after_a_further_fix_credits_nothing(pr, tmp_path):
    path, base, head, fix = pr
    budget = _pr_budget(base, head, fix, findings=[_finding("c9", head, 90, head)])
    _change(path, "after\n", T0 + timedelta(minutes=110))
    _reflect(path, tmp_path, _block(["c9"], fix), T0 + timedelta(minutes=120))
    assert _credit(path, budget) == set()


def test_the_deadline_refuses(pr, tmp_path):
    path, base, head, fix = pr
    budget = _pr_budget(base, head, fix, findings=[_finding("c9", head, 90, head)])
    _reflect(path, tmp_path, _block(["c9"], fix), T0 + timedelta(minutes=120))
    with pytest.raises(rr.Refused):
        rr.covered_late_keys(
            str(path),
            budget,
            rr.late_findings(budget, CUT),
            gate_lane=False,
            acceptance=None,
            deadline=0.0,
        )


# -- the after-fix rule (owner ruling 2026-10-13) ---------------------------------


def _opening_budget(fix_head="f" * 40):
    a = "a" * 40
    return _budget(
        rounds=[{"head": a, "reviews": [{"submitted_at": _iso(0), "finding_keys": ["c1", "c9"]}]}],
        commits=[{"sha": a, "parents": []}, {"sha": fix_head, "parents": [a]}],
        findings=[
            {"head": a, "submitted_at": _iso(0), "keys": ["c1"], "round_head": a},
            {"head": a, "submitted_at": _iso(90), "keys": ["c9"], "round_head": a},
        ],
    )


def test_a_round_opening_review_is_owed_only_once_a_fix_existed():
    fixed_at = T0 + timedelta(minutes=60)
    state = rr.owed_state(
        _opening_budget(), [], now=T0, cutover=CUT, fix_time=lambda head: fixed_at
    )
    # c1 came before the fix: the open-round check enforced it then.
    assert state["owed"] == ["c9"]
    nothing_fixed = rr.owed_state(
        _opening_budget(), [], now=T0, cutover=CUT, fix_time=lambda head: None
    )
    assert nothing_fixed["owed"] == []


def test_a_fix_time_that_is_needed_but_missing_is_unknown():
    assert rr.owed_state(_opening_budget(), [], now=T0, cutover=CUT)["owed"] is None


def test_a_round_opening_review_of_a_force_pushed_head_is_not_owed():
    budget = _opening_budget()
    budget["commits"] = [{"sha": "f" * 40, "parents": []}]
    state = rr.owed_state(budget, [], now=T0, cutover=CUT, fix_time=lambda head: T0)
    assert state["owed"] == []


def test_a_review_that_opened_no_round_is_owed_even_before_any_fix():
    budget = _opening_budget()
    budget["findings"].append(
        {"head": "a" * 40, "submitted_at": _iso(5), "keys": ["c5"], "round_head": "a" * 40}
    )
    state = rr.owed_state(budget, [], now=T0, cutover=CUT, fix_time=lambda head: None)
    assert state["owed"] == ["c5"]


def test_rounds_opened_at_the_same_time_are_ordered_by_head():
    a, b = "a" * 40, "b" * 40
    budget = _budget(
        rounds=[
            {"head": b, "reviews": [{"submitted_at": _iso(0)}]},
            {"head": a, "reviews": [{"submitted_at": _iso(0)}]},
        ]
    )
    assert rr.round_context(budget, b, T0).prior_heads == (a,)


def test_a_review_below_a_merge_of_two_rounds_answers_under_both():
    base, left, right, merge = "0" * 40, "1" * 40, "2" * 40, "3" * 40
    budget = _budget(
        rounds=[
            {"head": left, "reviews": [{"submitted_at": _iso(0)}]},
            {"head": right, "reviews": [{"submitted_at": _iso(5)}]},
        ],
        commits=[
            {"sha": base, "parents": []},
            {"sha": left, "parents": [base]},
            {"sha": right, "parents": [base]},
            {"sha": merge, "parents": [left, right]},
        ],
    )
    _, ancestors = rr._ancestry(budget)
    item = {"head": merge, "round_head": None, "at": T0 + timedelta(minutes=10)}
    contexts = rr._contexts(budget, item, T0 + timedelta(minutes=20), ancestors)
    assert [c.round_number for c in contexts] == [1, 2]
    # A linear history keeps only the newest round, never every older one.
    budget["commits"][2]["parents"] = [left]
    budget["commits"][3]["parents"] = [right]
    _, ancestors = rr._ancestry(budget)
    contexts = rr._contexts(budget, item, T0 + timedelta(minutes=20), ancestors)
    assert [c.round_number for c in contexts] == [2]


def test_an_empty_commit_is_no_fix_but_a_merge_is(pr, tmp_path):
    path, base, head, fix = pr
    _git(path, "checkout", "-q", "-b", "empty", head)
    _reflect(path, tmp_path, "not a reflection\n", T0 + timedelta(minutes=30))
    empty_tip = _git(path, "rev-parse", "HEAD")
    assert rr._fix_times(str(path), head, empty_tip) == []
    _git(path, "checkout", "-q", "-b", "merged", head)
    _git(path, "merge", "-q", "--no-ff", "-m", "merge", "empty", when=T0 + timedelta(minutes=40))
    merged = _git(path, "rev-parse", "HEAD")
    assert rr._fix_times(str(path), head, merged) == [T0 + timedelta(minutes=40)]


def test_an_amended_fix_keeps_its_author_time(pr, tmp_path):
    """The fix is amended after a late review: its author time stays before
    the review, so the review is still owed and still answerable."""
    path, base, head, fix = pr
    env_amend = T0 + timedelta(minutes=150)
    stamp = env_amend.strftime("%Y-%m-%dT%H:%M:%S+0000")
    subprocess.run(
        ["git", "-C", str(path), "commit", "-q", "--amend", "--no-edit"],
        check=True,
        env={**os.environ, "GIT_COMMITTER_DATE": stamp},
    )
    amended = _git(path, "rev-parse", "HEAD")
    budget = _pr_budget(base, head, amended, findings=[_finding("c9", head, 90, head)])
    budget["rounds"][0]["reviews"][0]["finding_keys"] = ["c9"]
    reader = rr.fix_time_reader(str(path), amended)
    assert reader(head) == T0 + timedelta(minutes=60)
    late = rr.late_findings(budget, CUT, reader)
    assert [item["key"] for item in late] == ["c9"]
    _reflect(path, tmp_path, _block(["c9"], amended), T0 + timedelta(minutes=160))
    assert rr.covered_late_keys(str(path), budget, late, gate_lane=False, acceptance=None) == {"c9"}


def test_the_latest_fix_decides_not_the_first(pr, tmp_path):
    """F1, then review c9, then F2 (its fix) with no reflection, then a
    reflection at F2: that is after c9's fix, so it answers nothing."""
    path, base, head, fix = pr
    second = _change(path, "z2\n", T0 + timedelta(minutes=100))
    budget = _pr_budget(base, head, fix, findings=[_finding("c9", head, 90, head)], current=second)
    budget["commits"].append({"sha": second, "parents": [fix]})
    _reflect(path, tmp_path, _block(["c9"], second), T0 + timedelta(minutes=120))
    assert _credit(path, budget) == set()
    # The control: the same reflection timing at F1, before F2, answers it.
    _git(path, "checkout", "-q", "-b", "at-f1", fix)
    _reflect(path, tmp_path, _block(["c9"], fix), T0 + timedelta(minutes=95))
    budget["current_head"] = fix
    assert _credit(path, budget) == {"c9"}


def test_a_review_between_two_fixes_is_owed_from_the_first(pr, tmp_path):
    path, base, head, fix = pr
    second = _change(path, "z2\n", T0 + timedelta(minutes=100))
    budget = _pr_budget(base, head, fix, findings=[_finding("c9", head, 80, head)], current=second)
    budget["commits"].append({"sha": second, "parents": [fix]})
    budget["rounds"][0]["reviews"][0]["finding_keys"] = ["c9"]
    reader = rr.fix_time_reader(str(path), second)
    assert [item["key"] for item in rr.late_findings(budget, CUT, reader)] == ["c9"]


def test_two_reflections_credit_the_union(pr, tmp_path):
    path, base, head, fix = pr
    budget = _pr_budget(
        base,
        head,
        fix,
        findings=[_finding("c8", head, 85, head), _finding("c9", head, 90, head)],
    )
    _reflect(path, tmp_path, _block(["c8"], fix), T0 + timedelta(minutes=120))
    _reflect(path, tmp_path, _block(["c9"], fix), T0 + timedelta(minutes=121))
    assert _credit(path, budget) == {"c8", "c9"}


def test_a_reflection_with_no_readable_time_credits_nothing(pr, tmp_path, monkeypatch):
    path, base, head, fix = pr
    budget = _pr_budget(base, head, fix, findings=[_finding("c9", head, 90, head)])
    sha = _reflect(path, tmp_path, _block(["c9"], fix), T0 + timedelta(minutes=120))
    monkeypatch.setattr(
        rr, "_log_reflections", lambda cwd: [(sha, "empty", _block(["c9"], fix), None)]
    )
    assert _credit(path, budget) == set()


def test_status_owes_late_keys_only_from_a_cutover(pr, tmp_path, monkeypatch):
    path, base, head, fix = pr
    budget = _pr_budget(base, head, fix, findings=[_finding("c9", head, 90, None)])
    budget["body"] = ""
    monkeypatch.setattr(rr, "pr_identity", lambda cwd: ("o/r", 1))
    import review_budget

    monkeypatch.setattr(review_budget, "evaluate_pr", lambda repo, number: budget)
    shipped = rr.status(str(path), now=T0)
    assert shipped["owed"] == [] and shipped["cutover"] is None
    monkeypatch.setattr(rr, "REFLECTION_CUTOVER_ISO", CUT)
    assert rr.status(str(path), now=T0)["owed"] == ["c9"]
    _reflect(path, tmp_path, _block(["c9"], fix), T0 + timedelta(minutes=120))
    assert rr.status(str(path), now=T0)["owed"] == []
    monkeypatch.setattr(rr, "REFLECTION_CUTOVER_ISO", None)
    monkeypatch.chdir(path)
    assert rr.main(["status", "--cutover", CUT]) == 0


# -- round-1 class (PR #3324): every input bound to the key's own round --------


def test_round_zero_owes_no_acceptance_and_no_evidence_window(pr, tmp_path):
    """Codex c4266451775: round 0 owes the grammar only. A reflection without
    the PR's acceptance Scope line, citing an audit written before the late
    review, still answers it."""
    path, base, head, fix = pr
    budget = _pr_budget(base, head, fix, findings=[_finding("c9", head, 90, None)], gate=True)
    budget["rounds"] = []
    audit = tmp_path / "old-audit.txt"
    audit.write_text("an audit from an earlier round\n")
    stamp = (T0 - timedelta(days=1)).timestamp()
    os.utime(audit, (stamp, stamp))
    text = _block(["c9"], fix, extra=[f"Audit-evidence: {audit} class-audit"])
    _reflect(path, tmp_path, text, T0 + timedelta(minutes=120))
    late = rr.late_findings(budget, CUT)
    credited = rr.covered_late_keys(
        str(path), budget, late, gate_lane=True, acceptance=["the cache survives a restart"]
    )
    assert credited == {"c9"}


def test_a_side_branchs_old_commit_is_not_a_fix_on_the_line(pr, tmp_path):
    """Codex c4266451758: a merged side branch's own commits are not fixes to
    the reviewed head; the merge that brings them in is."""
    path, base, head, fix = pr
    _git(path, "checkout", "-q", "-b", "side", base)
    (path / "g.txt").write_text("side\n")
    _git(path, "add", "g.txt")
    _git(path, "commit", "-q", "-m", "side work", when=T0 - timedelta(minutes=60))
    _git(path, "checkout", "-q", "-b", "line", head)
    _git(
        path, "merge", "-q", "--no-ff", "-m", "merge side", "side", when=T0 + timedelta(minutes=40)
    )
    merged = _git(path, "rev-parse", "HEAD")
    assert rr._fix_times(str(path), head, merged) == [T0 + timedelta(minutes=40)]
    # An opening review at +10 came before the merge: not owed after the fact.
    reader = rr.fix_time_reader(str(path), merged)
    assert reader(head) == T0 + timedelta(minutes=40)


def test_status_never_lets_the_open_rounds_coverage_clear_a_late_key(pr, monkeypatch):
    path, base, head, fix = pr
    budget = _pr_budget(base, head, fix, findings=[_finding("c9", head, 90, None)])
    budget.update(round_state="open", open_keys=["c2"], body="")
    monkeypatch.setattr(rr, "pr_identity", lambda cwd: ("o/r", 1))
    import review_budget

    monkeypatch.setattr(review_budget, "evaluate_pr", lambda repo, number: budget)
    monkeypatch.setattr(rr, "covered_keys", lambda *a, **k: {"c2", "c9"})
    monkeypatch.setattr(rr, "covered_late_keys", lambda *a, **k: set())
    assert rr.status(str(path), now=T0, cutover=CUT)["owed"] == ["c9"]


def test_a_review_of_a_head_outside_the_graph_is_never_credited(pr, tmp_path):
    """Owner ruling 2026-10-14: a force-push drops late debt. Even handed such
    a key directly, crediting never places it."""
    path, base, head, fix = pr
    gone = "7" * 40
    budget = _pr_budget(base, head, fix, findings=[_finding("c9", gone, 90, head)])
    _reflect(path, tmp_path, _block(["c9"], fix), T0 + timedelta(minutes=120))
    late = [{**budget["findings"][0], "key": "c9", "at": T0 + timedelta(minutes=90)}]
    assert rr.covered_late_keys(str(path), budget, late, gate_lane=False, acceptance=None) == set()


def test_an_unknown_round_order_makes_debt_unknown_never_a_crash():
    """A timeless round and a late key with no round head: placing the key
    needs the order, which is unknown, so owed is unknown (it raised before)."""
    budget = _budget(
        rounds=[{"head": "e" * 40, "reviews": []}],
        findings=[{"head": "f" * 40, "submitted_at": _iso(5), "keys": ["c7"], "round_head": None}],
    )
    assert rr.late_findings(budget, CUT) is None
    assert rr.owed_state(budget, [], now=T0, cutover=CUT)["owed"] is None


def test_a_legacy_head_reviewed_again_after_the_cutover_keeps_its_opening():
    """A legacy head marked before the round-rule cutover that also drew a
    later review opened at its first mark, so a round opened in between is
    numbered after it."""
    import review_budget

    cut = datetime.fromisoformat(review_budget.ROUND_RULE_CUTOVER_ISO)
    old, mid, legacy = cut - timedelta(days=1), cut + timedelta(hours=1), "e" * 40
    budget = _budget(
        rounds=[
            {"head": "a" * 40, "reviews": [{"submitted_at": mid.isoformat()}]},
            {"head": legacy, "reviews": [{"submitted_at": (mid + timedelta(hours=2)).isoformat()}]},
        ],
        marks=[{"head": legacy, "at": old.isoformat()}],
    )
    later = mid + timedelta(days=1)
    assert rr.round_context(budget, legacy, later) == rr.RoundContext(1, old.isoformat(), ())
    assert rr.round_context(budget, "a" * 40, later).round_number == 2


def test_no_review_of_a_head_outside_the_graph_is_owed():
    """Owner ruling 2026-10-14: a force-push drops late debt, whether the
    review opened a round or not."""
    gone = "7" * 40
    budget = _budget(
        findings=[{"head": gone, "submitted_at": _iso(5), "keys": ["c7"], "round_head": "a" * 40}]
    )
    assert rr.owed_state(budget, [], now=T0, cutover=CUT)["owed"] == []


def test_round_zero_still_refuses_an_escalating_reflection(pr, tmp_path):
    path, base, head, fix = pr
    budget = _pr_budget(base, head, fix, findings=[_finding("c9", head, 90, None)])
    budget["rounds"] = []
    escalating = _block(["c9"], fix).replace("Escalate: no", "Escalate: yes")
    _reflect(path, tmp_path, escalating, T0 + timedelta(minutes=120))
    assert _credit(path, budget) == set()


def test_a_reflection_made_later_never_withdraws_an_earlier_credit(pr, tmp_path):
    """Class labels of earlier rounds count only from reflections made before
    the one being judged: a round-1 reflection committed AFTER the late answer
    cannot turn its class into a recurring one and withdraw the credit."""
    path, base, head, fix = pr
    second = _change(path, "z2\n", T0 + timedelta(minutes=100))
    budget = _pr_budget(base, head, fix, findings=[_finding("c9", fix, 110, fix)], current=second)
    budget["rounds"] = [
        {"head": head, "reviews": [{"submitted_at": _iso(0)}]},
        {"head": fix, "reviews": [{"submitted_at": _iso(70)}]},
    ]
    budget["commits"].append({"sha": second, "parents": [fix]})
    premise = tmp_path / "premise.txt"
    premise.write_text(
        "Design-premise: SOUND-BUT-INFERIOR\n"
        "Expected before checking: it holds\n"
        "P1 TRUE — a\n"
        "P2 FALSE — b\n"
    )
    stamp = (T0 + timedelta(minutes=115)).timestamp()
    os.utime(premise, (stamp, stamp))
    answer = (
        _block(
            ["c9"],
            second,
            extra=[
                "Premise-check: P1 TRUE the store already dedups by key",
                "Premise-check: P2 FALSE replies are never top level",
                f"Premise-evidence: {premise} SOUND-BUT-INFERIOR",
            ],
        )
        .replace("Premise: LEAD", "Premise: SOUND-BUT-INFERIOR")
        .replace("Decision: close-class", "Decision: fix-instances")
    )
    _reflect(path, tmp_path, answer, T0 + timedelta(minutes=120))
    assert _credit(path, budget) == {"c9"}
    _reflect(path, tmp_path, _block(["c1"], head), T0 + timedelta(minutes=130))
    assert _credit(path, budget) == {"c9"}
