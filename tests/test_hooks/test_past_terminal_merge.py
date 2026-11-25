"""The past-terminal merge check (owner rulings 3f6ad6fb and cc17ee20, 2026-11-25).

Two halves:

- ``review_budget.evaluate_evidence`` returns ``terminal_head``: past terminal,
  the head of the 4th round by ``opened_at`` (the gate lane: its confirmed head),
  ``unknown`` when that cannot be placed, and None below terminal.
- ``git_push_guard._check_past_terminal`` blocks an agent merge whose
  ``--match-head-commit`` is not the terminal head (or a head that reaches it over
  empty commits only), and blocks at any round count when the budget is unreadable.
  The merge arm and the ``terminal-head`` row of ``--check-pr`` share it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from tests.test_hooks import test_merge_gate_characterization as ch

# The characterization module's autouse fixtures, re-exported so they apply here.
from tests.test_hooks.test_merge_gate_characterization import (  # noqa: F401
    _hermetic_pr_files,
    _pin_canonical_public_repo,
    _reset_merge_deadline,
)

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "scripts"))

import review_budget as rb  # noqa: E402
import review_findings as rf  # noqa: E402

G = ch._mod

H1, H2, H3, H4, H5, H6 = (c * 40 for c in "123456")
CUT = "2026-06-01T00:00:00+00:00"
P2 = "**<sub><sub>![P2 Badge](https://img.shields.io/badge/P2-yellow)</sub></sub>  Off by one**"
GATE_FILE = "scripts/hooks/git_push_guard.py"


def _at(hour: int) -> str:
    return f"2026-07-01T{hour:02d}:00:00Z"


def _finding(head: str, when: str) -> dict:
    """A post-cutover Codex review with one P2 finding: it opens a round on ``head``."""
    return {
        "login": rf.CODEX_LOGIN,
        "commit_id": head,
        "state": "COMMENTED",
        "submitted_at": when,
        "body": "",
        "top_level": [P2],
    }


def _ev(*, head, reviews=(), comments=(), files=("src/x.py",), commits=(H1, H2, H3, H4, H5, H6)):
    return rb.evaluate_evidence(
        current_head=head,
        commit_heads=commits,
        reviews=reviews,
        issue_comments=comments,
        changed_files=files,
        cutover=CUT,
    )


# -- terminal_head in the evaluator ------------------------------------------


def test_terminal_head_is_the_fourth_round_by_opened_at_not_list_order():
    # List (commit) order is H1..H5; the rounds OPENED in the order H1, H2, H5, H3, H4.
    reviews = (
        _finding(H1, _at(1)),
        _finding(H2, _at(2)),
        _finding(H5, _at(3)),
        _finding(H3, _at(4)),
        _finding(H4, _at(5)),
    )
    got = _ev(head=H6, reviews=reviews)
    assert got["status"] == "ok" and got["count"] == 5
    assert [r["head"] for r in got["rounds"]][3] == H4  # list position would say H4
    assert got["terminal_state"] == "past"
    assert got["terminal_head"] == H3


def test_terminal_head_at_exactly_four_rounds():
    reviews = tuple(_finding(h, _at(i)) for i, h in enumerate((H1, H2, H3, H4), start=1))
    got = _ev(head=H4, reviews=reviews)
    assert got["count"] == 4 and got["terminal_state"] == "past"
    assert got["terminal_head"] == H4


def test_below_the_limit_there_is_no_terminal_head():
    reviews = tuple(_finding(h, _at(i)) for i, h in enumerate((H1, H2, H3), start=1))
    got = _ev(head=H4, reviews=reviews)
    assert got["count"] == 3
    assert got["terminal_state"] == "below"
    assert got["terminal_head"] is None and got["terminal_error"] is None


def test_a_round_with_no_opened_at_is_unknown_never_a_guess():
    # A pending primary review before the cutover counts a legacy round with no time.
    pending = {"login": rf.CODEX_LOGIN, "commit_id": H1, "state": "PENDING"}
    reviews = (pending, *(_finding(h, _at(i)) for i, h in enumerate((H2, H3, H4), start=1)))
    got = _ev(head=H5, reviews=reviews)
    assert got["status"] == "ok" and got["count"] == 4
    assert got["rounds"][0]["opened_at"] is None
    assert got["terminal_state"] == "unknown"
    assert got["terminal_head"] is None
    assert got["terminal_error"] == "round_opened_at_missing"


def test_two_rounds_opened_at_the_same_instant_are_unknown():
    reviews = (
        _finding(H1, _at(1)),
        _finding(H2, _at(2)),
        _finding(H3, _at(3)),
        _finding(H4, _at(3)),
    )
    got = _ev(head=H5, reviews=reviews)
    assert got["count"] == 4
    assert got["terminal_state"] == "unknown"
    assert got["terminal_error"] == "round_opened_at_tied"


def _marker(head: str, when: str) -> dict:
    return {
        "login": "owner",
        "type": "User",
        "body": rb.confirmation_marker(head),
        "created_at": when,
    }


def _clean(head: str, when: str) -> dict:
    return {
        "login": rf.CODEX_LOGIN,
        "commit_id": head,
        "state": "APPROVED",
        "submitted_at": when,
        "body": "",
        "top_level": [],
    }


def test_the_gate_lane_returns_its_confirmed_head():
    reviews = (_finding(H1, _at(1)), _finding(H2, _at(2)), _clean(H3, _at(4)))
    comments = (_marker(H3, _at(3)),)
    at_confirmed = _ev(head=H3, reviews=reviews, comments=comments, files=(GATE_FILE,))
    assert at_confirmed["gate_surface"] is True
    assert at_confirmed["commit_approval_required"] is True
    assert at_confirmed["terminal_state"] == "past"
    assert at_confirmed["terminal_head"] == H3
    assert at_confirmed["confirmation_heads"] == [
        {"head": H3, "created_at": "2026-07-01T03:00:00+00:00"}
    ]
    # A fix commit after the confirmation keeps the confirmed head as terminal.
    later = _ev(head=H4, reviews=reviews, comments=comments, files=(GATE_FILE,))
    assert later["terminal_head"] == H3


def test_the_gate_lane_with_no_marker_falls_back_to_its_second_round():
    reviews = (_finding(H1, _at(1)), _finding(H3, _at(2)), _finding(H2, _at(3)))
    got = _ev(head=H4, reviews=reviews, files=(GATE_FILE,))
    assert got["count"] == 3 and got["terminal_state"] == "past"
    assert got["terminal_head"] == H3


def test_an_unreviewed_marker_never_becomes_the_terminal_head():
    # Past terminal by round count; a marker-only comment names a later head that no
    # reviewer looked at. It must not loosen the terminal head onto that head.
    reviews = (_finding(H1, _at(1)), _finding(H2, _at(2)), _finding(H3, _at(3)))
    comments = (_marker(H5, _at(4)),)
    got = _ev(head=H5, reviews=reviews, comments=comments, files=(GATE_FILE,))
    assert got["terminal_state"] == "past"
    assert got["terminal_head"] == H2


def test_the_gate_lane_before_its_confirmation_is_below_terminal():
    reviews = (_finding(H1, _at(1)), _finding(H2, _at(2)))
    got = _ev(head=H3, reviews=reviews, files=(GATE_FILE,))
    assert got["confirmation_exempt"] is True
    assert got["terminal_state"] == "below" and got["terminal_head"] is None


def test_unknown_evidence_carries_an_unknown_terminal():
    got = _ev(head="not-a-sha")
    assert got["status"] == "unknown"
    assert got["terminal_state"] == "unknown" and got["terminal_head"] is None


# -- the merge check ---------------------------------------------------------

T = "a" * 40  # the terminal head
BASE = "b" * 40  # its parent
MAIN = "c" * 40  # a commit on main
E1, E2 = "e" * 40, "f" * 40  # empty commits on top of T
N1, N2 = "1" * 39 + "a", "2" * 39 + "a"  # commits that changed a file
M = "3" * 39 + "a"  # a merge of main into T
F = "4" * 39 + "a"  # a force-pushed head with T's tree but not T's descendant

COMMITS = [
    {"sha": BASE, "parents": [], "tree": "t0" * 20},
    {"sha": T, "parents": [BASE], "tree": "t4" * 20},
    {"sha": E1, "parents": [T], "tree": "t4" * 20},
    {"sha": E2, "parents": [E1], "tree": "t4" * 20},
    {"sha": N1, "parents": [T], "tree": "t5" * 20},
    {"sha": N2, "parents": [E1], "tree": "t6" * 20},
    {"sha": M, "parents": [T, MAIN], "tree": "t7" * 20},
    {"sha": F, "parents": [BASE], "tree": "t4" * 20},
]


def _past(current: str, *, terminal: str = T, gate: bool = False) -> dict:
    return {
        "status": "ok",
        "terminal_state": "past",
        "terminal_head": terminal,
        "terminal_error": None,
        "current_head": current,
        "gate_surface": gate,
        "commits": COMMITS,
    }


def test_past_terminal_a_merge_at_the_terminal_head_passes():
    blocked, msg = G._check_past_terminal("7", "o/r", T, budget=_past(T))
    assert blocked is False, msg


@pytest.mark.parametrize(
    "head",
    [
        pytest.param(M, id="merge-of-main"),
        pytest.param(F, id="force-pushed-same-tree"),
        pytest.param(N1, id="extra-non-empty-commit"),
        pytest.param(N2, id="non-empty-after-empty"),
    ],
)
def test_past_terminal_any_other_head_blocks(head):
    blocked, msg = G._check_past_terminal("7", "o/r", head, budget=_past(head))
    assert blocked is True
    assert f"past round 4 the PR moved off its terminal head {T}" in msg
    assert "the owner's call" in msg
    assert "! gh pr merge 7" in msg and "sent back" in msg


@pytest.mark.parametrize("head", [E1, E2])
def test_past_terminal_only_empty_commits_after_the_terminal_head_pass(head):
    blocked, msg = G._check_past_terminal("7", "o/r", head, budget=_past(head))
    assert blocked is False, msg
    assert "empty commits only" in msg


def test_an_empty_commit_head_must_be_the_head_the_budget_read():
    # The walk starts from the budget's head; a merge bound to another sha blocks.
    blocked, _ = G._check_past_terminal("7", "o/r", E2, budget=_past(E1))
    assert blocked is True


def test_an_unbound_merge_past_terminal_blocks():
    blocked, _ = G._check_past_terminal("7", "o/r", "", budget=_past(T))
    assert blocked is True


def test_a_short_head_never_matches_the_terminal_head():
    blocked, _ = G._check_past_terminal("7", "o/r", T[:12], budget=_past(T))
    assert blocked is True


def test_the_head_comparison_ignores_case():
    blocked, msg = G._check_past_terminal("7", "o/r", T.upper(), budget=_past(T))
    assert blocked is False, msg


def test_the_gate_lane_names_its_own_limit():
    blocked, msg = G._check_past_terminal("7", "o/r", N1, budget=_past(N1, gate=True))
    assert blocked is True
    assert f"past round {rb.GATE_DISCOVERY_ROUND_LIMIT} the PR moved off" in msg


def test_an_unplaceable_terminal_head_blocks():
    budget = {
        "status": "ok",
        "terminal_state": "unknown",
        "terminal_head": None,
        "terminal_error": "round_opened_at_tied",
        "gate_surface": False,
    }
    blocked, msg = G._check_past_terminal("7", "o/r", T, budget=budget)
    assert blocked is True and "round_opened_at_tied" in msg


@pytest.mark.parametrize(
    "budget",
    [
        pytest.param(
            {"status": "unknown", "errors": ["graphql_unreadable"], "terminal_state": "unknown"},
            id="unknown",
        ),
        pytest.param({"status": "unknown", "terminal_state": "below"}, id="unknown-even-if-below"),
    ],
)
def test_an_unreadable_budget_blocks_below_terminal_too(budget):
    blocked, msg = G._check_past_terminal("7", "o/r", T, budget=budget)
    assert blocked is True
    assert "unreadable" in msg and "! gh pr merge 7" in msg


def test_below_terminal_readable_is_unchanged():
    budget = {"status": "ok", "terminal_state": "below", "terminal_head": None}
    blocked, msg = G._check_past_terminal("7", "o/r", N1, budget=budget)
    assert blocked is False, msg


def test_the_evaluator_output_drives_the_check_end_to_end():
    """The evaluator's real commit graph feeds the walk: an empty commit on top of
    the round-4 head passes, a commit that changed a file blocks."""
    commits = [
        {"sha": H1, "parents": [], "tree": "a1" * 20},
        {"sha": H2, "parents": [H1], "tree": "a2" * 20},
        {"sha": H3, "parents": [H2], "tree": "a3" * 20},
        {"sha": H4, "parents": [H3], "tree": "a4" * 20},
        {"sha": H5, "parents": [H4], "tree": "a4" * 20},  # empty
        {"sha": H6, "parents": [H5], "tree": "a6" * 20},  # changes a file
    ]
    reviews = tuple(_finding(h, _at(i)) for i, h in enumerate((H1, H2, H3, H4), start=1))
    empty = _ev(head=H5, reviews=reviews, commits=commits)
    assert empty["terminal_head"] == H4
    assert G._check_past_terminal("7", "o/r", H5, budget=empty)[0] is False
    moved = _ev(head=H6, reviews=reviews, commits=commits)
    assert G._check_past_terminal("7", "o/r", H6, budget=moved)[0] is True


# -- wiring: the merge arm and the --check-pr row ------------------------------


def _seam(monkeypatch, budget: dict) -> None:
    monkeypatch.setenv("_TEST_TERMINAL_BUDGET", json.dumps(budget))


def test_the_merge_arm_blocks_a_merge_off_the_terminal_head(monkeypatch, capsys):
    _seam(monkeypatch, _past(ch.HEAD, terminal=T))
    rc = ch._run(monkeypatch, ch._merge_cmd())
    err = capsys.readouterr().err
    assert rc == 2
    assert f"past round 4 the PR moved off its terminal head {T}" in err


def test_the_merge_arm_allows_a_merge_at_the_terminal_head(monkeypatch):
    _seam(monkeypatch, _past(ch.HEAD, terminal=ch.HEAD))
    assert ch._run(monkeypatch, ch._merge_cmd()) == 0


def test_the_merge_arm_blocks_an_unreadable_budget(monkeypatch, capsys):
    _seam(monkeypatch, {"status": "unknown", "errors": ["graphql_unreadable"]})
    rc = ch._run(monkeypatch, ch._merge_cmd())
    assert rc == 2
    assert "review budget is unreadable" in capsys.readouterr().err


def test_no_sigil_waives_the_check(monkeypatch):
    _seam(monkeypatch, _past(ch.HEAD, terminal=T))
    cmd = ch._merge_cmd(trailer="# review-override stale-review-override ci-override")
    assert ch._run(monkeypatch, cmd) == 2


def test_the_report_row_blocks_and_counts_in_the_verdict(monkeypatch, capsys):
    ch._report_env(monkeypatch, scheduled=ch._scheduled_marker(ch.HEAD))
    _seam(monkeypatch, _past(ch.HEAD, terminal=T))
    rc = G.check_pr_report("100", repo=ch.REPO)
    out = capsys.readouterr().out
    row = next(ln for ln in out.splitlines() if ln.startswith("terminal-head"))
    assert row.startswith("terminal-head  : BLOCK — past round 4 the PR moved off"), row
    assert "merge-with" not in out
    assert rc == 1


def test_the_report_row_is_ok_below_terminal(monkeypatch, capsys):
    ch._report_env(monkeypatch, scheduled=ch._scheduled_marker(ch.HEAD))
    rc = G.check_pr_report("100", repo=ch.REPO)
    out = capsys.readouterr().out
    row = next(ln for ln in out.splitlines() if ln.startswith("terminal-head"))
    assert row == "terminal-head  : ok (below terminal)"
    assert rc == 0, out
