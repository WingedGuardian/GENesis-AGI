"""The review loop's commit-side end (#4133, rework PR 3 of #4368): past round 4,
once the newest round a fix produced (round 5 or later) draws no floor finding,
the commit gate stops content commits. Read from the budget alone, asked before
the owed read. One test per row of the plan's writer-state table."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import review_budget as rb  # noqa: E402
import review_findings as rf  # noqa: E402
import review_past_terminal as pt  # noqa: E402
import review_reflection as rr  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "loop_end_hook", ROOT / "scripts" / "review_enforcement_commit.py"
)
_gate = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(_gate)

CUT = "2026-11-01T00:00:00+00:00"
T0 = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
CODEX = rf.CODEX_LOGIN
P1 = "**<sub><sub>![P1 Badge](https://img.shields.io/badge/P1-orange)</sub></sub>  Bug**"
P2 = "**<sub><sub>![P2 Badge](https://img.shields.io/badge/P2-yellow)</sub></sub>  Nit**"
H = [f"{i:040x}" for i in range(1, 12)]
TREE = [f"{i:040x}"[::-1] for i in range(1, 12)]
OUTSIDE = "f" * 40
LATER = T0 + timedelta(days=3)


def _iso(when: datetime) -> str:
    return when.isoformat().replace("+00:00", "Z")


def _rv(head, when, rid, badge=P2):
    return {
        "login": CODEX,
        "commit_id": head,
        "state": "COMMENTED",
        "submitted_at": _iso(when),
        "body": "",
        "top_level": [badge],
        "id": rid,
        "top_level_ids": [rid * 10],
    }


def _row(i, parents, tree, committed=None):
    when = T0 + timedelta(hours=i) - timedelta(minutes=30) if committed is None else committed
    return {"sha": H[i], "parents": parents, "tree": tree, "committed_at": _iso(when)}


def _budget(commits, reviews, head, *, gate=False):
    budget = rb.evaluate_evidence(
        current_head=head,
        commit_heads=commits,
        reviews=reviews,
        issue_comments=(),
        changed_files=("scripts/review_budget.py",) if gate else ("src/x.py",),
        cutover="2026-06-01T00:00:00+00:00",
        external_identity_templates=(),
    )
    assert budget["status"] == "ok", budget
    budget.update(pr=99, repo="owner/repo")
    return budget


def _fix_rounds(n, *, last=P2, extra_reviews=(), extra_commits=()):
    """n rounds, each on a commit a fix produced (its tree changes), one Codex
    review an hour apart; the last round's badge is ``last``."""
    commits = [_row(0, [], TREE[0])] + [_row(i, [H[i - 1]], TREE[i]) for i in range(1, n)]
    commits += list(extra_commits)
    reviews = [_rv(H[i], T0 + timedelta(hours=i), 10 + i) for i in range(n - 1)]
    reviews.append(_rv(H[n - 1], T0 + timedelta(hours=n - 1), 10 + n - 1, badge=last))
    reviews += list(extra_reviews)
    head = commits[-1]["sha"]
    return _budget(commits, reviews, head)


def _ended(budget, *, now=LATER, cutover=CUT):
    return pt.loop_ended(
        pt.round_records(budget),
        pt.late_floor(budget),
        gate_lane=bool(budget.get("gate_surface")),
        now=now,
        cutover=cutover,
    )


# -- loop_ended, from the budget alone ----------------------------------------


@pytest.mark.parametrize(("n", "want"), [(3, False), (4, False), (5, True), (7, True)])
def test_the_loop_ends_from_round_five_with_no_floor_finding(n, want):
    """Round 4 never ends it; round 5 or later with no floor finding does."""
    assert _ended(_fix_rounds(n)) is want


def test_a_floor_finding_on_the_newest_round_keeps_fixing_open():
    assert _ended(_fix_rounds(5, last=P1)) is False


def test_an_unreadable_level_on_the_newest_round_cannot_tell():
    budget = _fix_rounds(5)
    for item in budget["findings"]:
        if item["round_head"] == H[4]:
            item["blocking"] = [None]
    assert _ended(budget) is None


def test_nothing_ends_on_the_gate_lane_before_the_cutover_or_with_none():
    budget = _fix_rounds(6)
    assert _ended(budget, cutover=None) is False
    assert _ended(budget, cutover="2026-12-01T00:00:00+00:00") is False
    budget["gate_surface"] = True
    assert _ended(budget) is False


def test_unreadable_inputs_cannot_tell():
    budget = _fix_rounds(5)
    assert pt.loop_ended(None, (), gate_lane=False, now=LATER, cutover=CUT) is None
    assert (
        pt.loop_ended(pt.round_records(budget), None, gate_lane=False, now=LATER, cutover=CUT)
        is None
    )
    budget["findings"] = "garbled"
    assert pt.late_floor(budget) is None


def test_the_shipped_cutover_is_read_at_call_time(monkeypatch):
    budget = _fix_rounds(5)
    monkeypatch.setattr(pt, "PAST_TERMINAL_CUTOVER_ISO", None)
    assert (
        pt.loop_ended(pt.round_records(budget), pt.late_floor(budget), gate_lane=False, now=LATER)
        is False
    )
    monkeypatch.setattr(pt, "PAST_TERMINAL_CUTOVER_ISO", CUT)
    assert (
        pt.loop_ended(pt.round_records(budget), pt.late_floor(budget), gate_lane=False, now=LATER)
        is True
    )


# -- which rounds count toward the thresholds (owner ruling 2796375a) ---------


def test_merge_of_main_rounds_never_advance_the_thresholds():
    """Three fix rounds, then two rounds a merge of main produced: the budget
    reads round 5, the rules read fix round 3, so nothing ends and nothing is
    past round 3."""
    commits = [_row(0, [], TREE[0]), _row(1, [H[0]], TREE[1]), _row(2, [H[1]], TREE[2])]
    commits += [_row(3, [H[2], OUTSIDE], TREE[3]), _row(4, [H[3], OUTSIDE], TREE[4])]
    reviews = [_rv(H[i], T0 + timedelta(hours=i), 10 + i) for i in range(5)]
    budget = _budget(commits, reviews, H[4])
    records = pt.round_records(budget)
    assert budget["count"] == 5
    assert [records[h].rules_round for h in H[:5]] == [1, 2, 3, 3, 3]
    assert _ended(budget) is False
    assert pt._past_terminal_pr(records, LATER, False, CUT) is None


def test_a_fix_round_numbered_four_after_a_merge_round_is_fix_round_three():
    """Fix, fix, merge of main, fix: the budget reads round 4, the rules read
    fix round 3, so the Prediction lines are not yet owed."""
    commits = [_row(0, [], TREE[0]), _row(1, [H[0]], TREE[1])]
    commits += [_row(2, [H[1], OUTSIDE], TREE[2]), _row(3, [H[2]], TREE[3])]
    reviews = [_rv(H[i], T0 + timedelta(hours=i), 10 + i) for i in range(4)]
    records = pt.round_records(_budget(commits, reviews, H[3]))
    newest = records[H[3]]
    assert (newest.number, newest.rules_round) == (4, 3)
    assert pt._past_terminal_pr(records, LATER, False, CUT) is None


def test_an_unknown_fix_never_moves_a_pr_toward_the_end():
    """Audit SF-1: one unreadable tree must not count rounds 3 and 4 as fixes
    and end the loop of a PR with three real fix rounds (unknown fails open)."""
    commits = [_row(0, [], TREE[0]), _row(1, [H[0]], TREE[1]), _row(2, [H[1]], None)]
    commits += [_row(3, [H[2]], TREE[1]), _row(4, [H[3]], TREE[4])]
    reviews = [_rv(H[i], T0 + timedelta(hours=i), 10 + i) for i in range(5)]
    budget = _budget(commits, reviews, H[4])
    records = pt.round_records(budget)
    assert records[H[2]].advanced_by_fix is None
    assert records[H[4]].rules_round < pt.JUDGE_FROM
    assert _ended(budget) is not True


def test_the_rules_stay_silent_at_budget_round_five_when_it_is_fix_round_four():
    """E, H and F read ``rules_round``: a merge round makes budget round 5 fix
    round 4, so only the Prediction lines (P) bind a fix-now."""
    commits = [_row(0, [], TREE[0]), _row(1, [H[0]], TREE[1]), _row(2, [H[1], OUTSIDE], TREE[2])]
    commits += [_row(3, [H[2]], TREE[3]), _row(4, [H[3]], TREE[4])]
    reviews = [_rv(H[i], T0 + timedelta(hours=i), 10 + i) for i in range(5)]
    records = pt.round_records(_budget(commits, reviews, H[4]))
    newest = records[H[4]]
    assert (newest.number, newest.rules_round) == (5, 4)
    parsed = rr.parse(
        "\n".join(
            [
                f"Round-reflection: keys=c14 head={'a' * 40}",
                "Class: unchecked boundary value = 1",
                "Premise: LEAD",
                "Premise-why: the bound is checked in one place and trusted in another",
                "Impossible: route every reader through the one checked helper",
                "Alternative: none found: searched scripts/ for an existing checked helper",
                "Scope: covered: the bound holds at the edge (tests/test_x.py)",
                "Decision: fix-instances",
                "Decision-why: the same unchecked read appears at both call sites",
                "Disposition: c14 fix-now test=1",
                "Prediction: merges at round 6 because the class is closed",
                "Confidence: 97% falsified-by any new finding in that class",
                "Escalate: no",
            ]
        )
        + "\n"
    )
    assert parsed.ok, parsed.problems
    assert pt.problems(parsed, newest, records, gate_lane=False, made_at=LATER, cutover=CUT) == []


def test_a_dropped_head_never_advances_the_thresholds():
    gone = "e" * 40
    commits = [_row(i, [H[i - 1]] if i else [], TREE[i]) for i in range(4)]
    reviews = [_rv(H[i], T0 + timedelta(hours=i), 10 + i) for i in range(3)]
    reviews.insert(3, _rv(gone, T0 + timedelta(hours=2, minutes=30), 30))
    reviews.append(_rv(H[3], T0 + timedelta(hours=3), 13))
    budget = _budget(commits, reviews, H[3])
    records = pt.round_records(budget)
    assert budget["count"] == 5 and records[H[3]].rules_round == 4
    assert _ended(budget) is False


def test_a_merge_round_after_the_end_is_not_newest_and_the_loop_stays_ended():
    budget = _fix_rounds(
        5,
        extra_commits=[_row(5, [H[4], OUTSIDE], TREE[5])],
        extra_reviews=[_rv(H[5], T0 + timedelta(hours=5), 15)],
    )
    assert pt.round_records(budget)[H[5]].advanced_by_fix is False
    assert _ended(budget) is True


def test_a_floor_finding_on_a_merge_round_after_the_end_reopens_fixing():
    budget = _fix_rounds(
        5,
        extra_commits=[_row(5, [H[4], OUTSIDE], TREE[5])],
        extra_reviews=[_rv(H[5], T0 + timedelta(hours=5), 15, badge=P1)],
    )
    assert _ended(budget) is False


# -- late floor findings ------------------------------------------------------


def test_a_late_floor_finding_after_the_newest_head_commit_reopens_fixing():
    late = _rv(H[1], T0 + timedelta(hours=4, minutes=10), 40, badge=P1)
    assert _ended(_fix_rounds(5, extra_reviews=[late])) is False


def test_a_late_finding_below_the_floor_does_not_reopen():
    late = _rv(H[1], T0 + timedelta(hours=4, minutes=10), 40)
    assert _ended(_fix_rounds(5, extra_reviews=[late])) is True


def test_a_floor_finding_older_than_the_newest_head_commit_stays_closed():
    """The window opens at the newest head's commit (spec Mechanism 4): an
    earlier floor finding was answerable before that fix, so it does not
    reopen fixing. Stated residual: one posted just before that commit."""
    budget = _fix_rounds(5)
    records = pt.round_records(budget)
    newest = records[H[4]]
    early = ((newest.head_committed_at - timedelta(minutes=1), True, "c999"),)
    assert pt._late_floor_open(newest, early, LATER) == []
    assert pt._late_floor_open(newest, ((None, True, "c999"),), LATER) == ["c999"]


def test_an_unkeyed_late_floor_finding_still_reopens_fixing():
    row = {"keys": [None], "blocking": [True], "submitted_at": _iso(T0 + timedelta(hours=5))}
    budget = _fix_rounds(5)
    budget["findings"].append({**row, "head": H[1], "round_head": H[1], "late": True})
    assert (T0 + timedelta(hours=5), True, "an unkeyed finding") in pt.late_floor(budget)
    assert _ended(budget) is False


GONE = "e" * 40


def test_a_floor_finding_on_a_dropped_head_never_reopens_the_loop():
    """Round 1 of #4868 (Devin): a force-push dropped head X; a P1 on X after
    the newest head's commit is an obsolete review, as ``late_findings`` reads
    it, so the loop stays ended."""
    late = _rv(GONE, T0 + timedelta(hours=4, minutes=10), 50, badge=P1)
    budget = _fix_rounds(5, extra_reviews=[late])
    assert any(item["head"] == GONE for item in budget["findings"])
    assert all(key != "c500" for _, _, key in pt.late_floor(budget))
    assert _ended(budget) is True


def test_a_dropped_head_finding_with_no_round_is_skipped_too():
    budget = _fix_rounds(5)
    row = {"head": GONE, "round_head": None, "keys": ["c600"], "blocking": [True],
           "submitted_at": _iso(T0 + timedelta(hours=5)), "late": True}
    budget["findings"].append(row)
    assert _ended(budget) is True


def test_a_malformed_row_on_a_dropped_head_does_not_make_the_end_unknown():
    budget = _fix_rounds(5)
    budget["findings"].append({"head": GONE, "keys": "garbled", "submitted_at": None})
    assert pt.late_floor(budget) is not None
    assert _ended(budget) is True


def test_an_unreadable_head_commit_time_opens_every_late_floor_finding():
    budget = _fix_rounds(5)
    newest = pt.round_records(budget)[H[4]]
    blind = pt.RoundRecord(newest.head, newest.number, newest.opened_at, newest.findings)
    assert blind.head_committed_at is None
    assert pt._late_floor_open(blind, ((T0, True, "c1"),), LATER) == ["c1"]


# -- the advisory when the rules are off ---------------------------------------


def _unknown_round(n):
    commits = [_row(0, [], TREE[0])] + [_row(i, [H[i - 1]], TREE[i]) for i in range(1, n)]
    commits[-1]["tree"] = None  # the newest fix cannot be read
    commits[-2]["tree"] = None
    reviews = [_rv(H[i], T0 + timedelta(hours=i), 10 + i) for i in range(n)]
    return _budget(commits, reviews, H[n - 1])


def test_the_round_the_rules_switch_off_at_is_named_only_when_it_matters():
    budget = _unknown_round(5)
    records = pt.round_records(budget)
    assert pt.newest_under_rules(records, LATER) is pt.UNKNOWN
    assert pt.rules_off_round(records, gate_lane=False, now=LATER, cutover=CUT).number == 5
    assert pt.rules_off_round(records, gate_lane=True, now=LATER, cutover=CUT) is None
    assert pt.rules_off_round(records, gate_lane=False, now=LATER, cutover=None) is None
    future = "2026-12-01T00:00:00+00:00"
    assert pt.rules_off_round(records, gate_lane=False, now=LATER, cutover=future) is None
    small = pt.round_records(_unknown_round(3))
    assert pt.rules_off_round(small, gate_lane=False, now=LATER, cutover=CUT) is None
    assert _ended(budget) is None


@pytest.fixture
def quiet(monkeypatch):
    monkeypatch.setattr(_gate, "_ADVISORIES", [])
    monkeypatch.setattr(pt, "PAST_TERMINAL_CUTOVER_ISO", CUT)
    return _gate._ADVISORIES


def test_the_commit_gate_says_when_the_rules_are_off(quiet):
    _gate._loop_end(_unknown_round(5))
    assert len(quiet) == 1 and "rules are OFF for PR #99" in quiet[0] and "round 5" in quiet[0]


def test_the_commit_gate_is_silent_while_the_rules_read_the_pr(quiet):
    assert _gate._loop_end(_fix_rounds(5)).ended is True
    assert _gate._loop_end(_fix_rounds(3)).ended is False
    assert quiet == []


def test_a_loop_end_that_cannot_be_read_is_not_ended(quiet, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("garbled")

    monkeypatch.setattr(pt, "round_records", boom)
    assert _gate._loop_end(_fix_rounds(5)).ended is False
    assert "could not be read" in quiet[0]


# -- the commit gate ------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "-c", "init.defaultBranch=main", "init", "-q")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "tester")
    (path / "f.py").write_text("value = 1\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", "base")
    _git(path, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(path, "checkout", "-qb", "feature")
    (path / "f.py").write_text("value = 2\n")
    _git(path, "commit", "-qam", "the reviewed head")
    return path


def _rule(command, repo, budget, *, deadline=None):
    segs, _ = _gate.analyze_checked(command)
    commit_segs = [s for s in segs if _gate.git_subcommand(s.argv) == "commit"]
    return _gate._reflection_rule(
        segs,
        commit_segs,
        command,
        str(repo),
        budget,
        hook_deadline=time.monotonic() + 9 if deadline is None else deadline,
    )


def _reflection(tmp_path, how="file issue=#1", key="c14"):
    text = "\n".join(
        [
            f"Round-reflection: keys={key} head={'a' * 40}",
            "Class: unchecked boundary value = 1",
            "Premise: LEAD",
            "Premise-why: the bound is checked in one place and trusted in another",
            "Impossible: route every reader through the one checked helper",
            "Alternative: none found: searched scripts/ for an existing checked helper",
            "Scope: covered: the bound holds at the edge (tests/test_x.py)",
            "Decision: fix-instances" if how.startswith("fix") else "Decision: send-back",
            "Decision-why: the same unchecked read appears at both call sites",
            f"Disposition: {key} {how}",
            "Prediction: merges at round 6 because the class is closed",
            "Confidence: 97% falsified-by any new finding in that class",
            "Prediction-held: no the round drew no floor finding",
            "Escalate: no",
        ]
    )
    path = tmp_path / "reflection.md"
    path.write_text(text + "\n")
    return path


@pytest.mark.parametrize(
    "owed",
    [{"owed": []}, {"owed": None}, RuntimeError("owed read failed"), {"owed": ["c14"]}],
)
def test_a_content_commit_is_denied_in_an_ended_loop_whatever_the_owed_read_says(
    quiet, monkeypatch, repo, owed
):
    def read(*a, **k):
        if isinstance(owed, Exception):
            raise owed
        return owed

    monkeypatch.setattr(rr, "owed_from_budget", read)
    (repo / "f.py").write_text("value = 3\n")
    _git(repo, "add", "f.py")
    verdict, message = _rule('git commit -m "fix"', repo, _fix_rounds(5))
    assert verdict == "deny"
    assert "review loop of PR #99 is over" in message and "round 5" in message
    assert "neither an empty reflection" in message


def test_the_deny_names_every_exit(quiet, monkeypatch, repo):
    monkeypatch.setattr(rr, "owed_from_budget", lambda *a, **k: {"owed": []})
    _, message = _rule('git commit -m "fix"', repo, _fix_rounds(5))
    for part in (
        "--check-pr 99",
        "send the PR back",
        "file it as an issue",
        "filing reflection first",
    ):
        assert part in message
    assert "ask the owner" not in message


def test_the_end_never_reads_a_reflection(quiet, monkeypatch, repo):
    """Acceptance 3: nothing on the loop end's path reads a committed reflection."""
    monkeypatch.setattr(rr, "_log_reflections", lambda *a, **k: pytest.fail("read a reflection"))
    monkeypatch.setattr(rr, "owed_from_budget", lambda *a, **k: {"owed": []})
    (repo / "f.py").write_text("value = 3\n")
    _git(repo, "add", "f.py")
    assert _rule('git commit -m "fix"', repo, _fix_rounds(5))[0] == "deny"


def test_an_empty_filing_reflection_passes_the_ended_branch(quiet, monkeypatch, repo, tmp_path):
    monkeypatch.setattr(rr, "owed_from_budget", lambda *a, **k: {"owed": []})
    path = _reflection(tmp_path)
    verdict, _ = _rule(
        f"git commit --allow-empty --cleanup=verbatim -F {path}", repo, _fix_rounds(5)
    )
    assert verdict == "continue"


def test_a_reflection_that_fixes_now_is_refused_in_an_ended_loop(
    quiet, monkeypatch, repo, tmp_path
):
    monkeypatch.setattr(rr, "owed_from_budget", lambda *a, **k: {"owed": []})
    path = _reflection(tmp_path, how="fix-now test=1")
    verdict, message = _rule(
        f"git commit --allow-empty --cleanup=verbatim -F {path}", repo, _fix_rounds(5)
    )
    assert verdict == "deny" and "nothing can be fixed now" in message


def test_admission_refuses_a_fix_now_when_the_loop_is_over(quiet, monkeypatch, repo, tmp_path):
    path = _reflection(tmp_path, how="fix-now test=1")
    command = f"git commit --allow-empty --cleanup=verbatim -F {path}"
    segs, _ = _gate.analyze_checked(command)
    problem = _gate._reflection_problem(
        segs, command, str(repo), _fix_rounds(5), rr, deadline=time.monotonic() + 5, ended=True
    )
    assert problem is not None and "nothing can be fixed now" in problem


def test_a_merge_of_main_passes_the_ended_branch(quiet, monkeypatch, repo):
    monkeypatch.setattr(rr, "owed_from_budget", lambda *a, **k: {"owed": []})
    _merging_main(repo)
    assert _rule("git commit --no-edit", repo, _fix_rounds(5)) == ("continue", None)


def test_a_merge_that_carries_a_fix_is_denied(quiet, monkeypatch, repo):
    monkeypatch.setattr(rr, "owed_from_budget", lambda *a, **k: {"owed": []})
    _git(repo, "checkout", "-q", "main")
    (repo / "g.py").write_text("other = 1\n")
    _git(repo, "add", "g.py")
    _git(repo, "commit", "-qm", "main moves")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(repo, "checkout", "-q", "feature")
    _git(repo, "merge", "--no-ff", "--no-commit", "origin/main")
    (repo / "f.py").write_text("value = 9\n")
    _git(repo, "add", "f.py")
    assert _rule("git commit --no-edit", repo, _fix_rounds(5))[0] == "deny"


def _merging_main(repo):
    _git(repo, "checkout", "-q", "main")
    (repo / "g.py").write_text("other = 1\n")
    _git(repo, "add", "g.py")
    _git(repo, "commit", "-qm", "main moves")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(repo, "checkout", "-q", "feature")
    _git(repo, "merge", "--no-ff", "--no-commit", "origin/main")


def test_a_broken_reflection_is_told_why_in_an_ended_loop(quiet, monkeypatch, repo, tmp_path):
    """Audit SF-2: the deny names the reflection's own problem."""
    monkeypatch.setattr(rr, "owed_from_budget", lambda *a, **k: {"owed": []})
    path = _reflection(tmp_path)
    path.write_text(path.read_text().replace("Escalate: no\n", ""))
    _, message = _rule(
        f"git commit --allow-empty --cleanup=verbatim -F {path}", repo, _fix_rounds(5)
    )
    assert "as a reflection: the reflection does not pass its grammar" in message


def test_a_settled_round_does_not_say_wait(quiet, monkeypatch, repo):
    monkeypatch.setattr(rr, "owed_from_budget", lambda *a, **k: {"owed": []})
    budget = _fix_rounds(5)
    _, message = _rule('git commit -m "fix"', repo, budget)
    assert "That round has settled." in message and "do not merge before" not in message
    end = _gate._LoopEnd(True, 5, datetime.now(UTC) + timedelta(minutes=10))
    assert "do not merge before then" in _gate._ended_message(budget, end)
    assert "cannot be read" in _gate._ended_message(budget, _gate._LoopEnd(True, 5, None))


def test_a_merge_of_main_while_keys_are_owed_meets_the_owed_rule(quiet, monkeypatch, repo):
    """The ended branch only adds refusals: with keys owed the owed rule denies
    the merge as on main, and the session reflects first."""
    state = {"owed": ["c14"], "settled": True, "late_keys": []}
    monkeypatch.setattr(rr, "owed_from_budget", lambda *a, **k: state)
    _merging_main(repo)
    verdict, message = _rule("git commit --no-edit", repo, _fix_rounds(5))
    assert verdict == "deny" and "owes a round reflection" in message


def test_an_ended_check_that_raises_keeps_the_deny(quiet, monkeypatch, repo):
    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(_gate, "_ended_commit_problem", boom)
    monkeypatch.setattr(rr, "owed_from_budget", lambda *a, **k: {"owed": []})
    verdict, message = _rule('git commit -m "fix"', repo, _fix_rounds(5))
    assert verdict == "deny" and "could not be checked (RuntimeError); run it again" in message


def test_a_merge_check_out_of_time_keeps_the_deny(quiet, monkeypatch, repo):
    monkeypatch.setattr(rr, "owed_from_budget", lambda *a, **k: {"owed": []})
    _merging_main(repo)
    deadline = time.monotonic() + _gate._REFLECTION_RESERVE
    verdict, message = _rule("git commit --no-edit", repo, _fix_rounds(5), deadline=deadline)
    assert verdict == "deny" and "ran out of time; run it again" in message


def test_advisory_mode_says_what_it_would_have_denied(quiet, monkeypatch, repo):
    monkeypatch.setattr(rr, "owed_from_budget", lambda *a, **k: {"owed": []})
    monkeypatch.setattr(_gate, "_reflection_gate_mode", lambda: "advisory")
    (repo / "f.py").write_text("value = 3\n")
    _git(repo, "add", "f.py")
    assert _rule('git commit -m "fix"', repo, _fix_rounds(5)) == ("continue", None)
    assert any("NOT enforced" in a and "is over" in a for a in quiet)


@pytest.mark.parametrize("n", [3, 4])
def test_before_the_end_main_s_path_is_unchanged(quiet, monkeypatch, repo, n):
    monkeypatch.setattr(rr, "owed_from_budget", lambda *a, **k: {"owed": []})
    (repo / "f.py").write_text("value = 3\n")
    _git(repo, "add", "f.py")
    assert _rule('git commit -m "fix"', repo, _fix_rounds(n)) == ("continue", None)


def test_the_3004_replay_ends_the_loop_from_round_five():
    """Acceptance 1: PR #3004's saved budget (the PR that ran to round 9),
    replayed a minute after each round opened, with the cutover before round 1:
    not over through round 4, over from round 5's opening, reopened by round
    7's floor finding, over again from round 8."""
    import json

    budget = json.loads(
        (Path(__file__).parent / "fixtures" / "review_budget_3004.json").read_text()
    )
    records = pt.round_records(budget)
    late = pt.late_floor(budget)
    first = min(r.opened_at for r in records.values())
    cut = (first - timedelta(minutes=1)).isoformat()
    got = {
        r.number: pt.loop_ended(
            records, late, gate_lane=False, now=r.opened_at + timedelta(minutes=1), cutover=cut
        )
        for r in records.values()
    }
    assert got == {
        1: False,
        2: False,
        3: False,
        4: False,
        5: True,
        6: True,
        7: False,
        8: True,
        9: True,
    }


def test_the_cutover_when_set_is_a_bounded_constant():
    """Set in this PR's last commit to about a day after its deploy (owner
    2fd36db6): it must parse and lie within a week of the time it was set."""
    value = pt.PAST_TERMINAL_CUTOVER_ISO
    assert value is not None, "the rules ship on with the loop's end"
    when = datetime.fromisoformat(value)
    assert when.tzinfo is not None
    assert datetime(2026, 11, 17, tzinfo=UTC) < when < datetime(2026, 11, 30, tzinfo=UTC)
