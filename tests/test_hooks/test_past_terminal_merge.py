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


@pytest.mark.parametrize("review_hour", [2, 3], ids=["review-before-marker", "same-instant"])
def test_a_review_from_before_the_marker_confirms_nothing(review_hour):
    # H3 was reviewed (clean) before, or at the instant, its confirmation marker
    # was posted. That review answers no confirmation request, so the terminal
    # head stays the round-2 head.
    reviews = (_finding(H1, _at(0)), _finding(H2, _at(1)), _clean(H3, _at(review_hour)))
    comments = (_marker(H3, _at(3)),)
    got = _ev(head=H3, reviews=reviews, comments=comments, files=(GATE_FILE,))
    assert got["terminal_state"] == "past"
    assert got["terminal_head"] == H2


def test_a_marker_for_an_earlier_reviewed_head_cannot_outrank_the_real_one():
    # Someone posts a marker naming H2, which Codex reviewed BEFORE that comment.
    # Then the real confirmation for H3 is posted and reviewed. H3 is terminal.
    reviews = (_finding(H1, _at(1)), _finding(H2, _at(2)), _clean(H3, _at(6)))
    comments = (_marker(H2, _at(4)), _marker(H3, _at(5)))
    got = _ev(head=H3, reviews=reviews, comments=comments, files=(GATE_FILE,))
    assert got["terminal_head"] == H3


def test_a_thread_reply_after_the_marker_answers_nothing():
    # H3 was approved before its marker. Afterwards Codex only replies inside a
    # thread, which GitHub records as an empty COMMENTED review on H3. That wrapper
    # is no review, so it must not answer the marker: the round-2 head stays terminal.
    reply = {
        "login": rf.CODEX_LOGIN,
        "commit_id": H3,
        "state": "COMMENTED",
        "submitted_at": _at(5),
        "body": "",
        "top_level": [],
    }
    reviews = (_finding(H1, _at(0)), _finding(H2, _at(1)), _clean(H3, _at(2)), reply)
    comments = (_marker(H3, _at(3)),)
    got = _ev(head=H3, reviews=reviews, comments=comments, files=(GATE_FILE,))
    assert got["terminal_head"] == H2


def test_a_real_commented_review_after_the_marker_still_answers_it():
    # The positive twin of the thread-reply case: a COMMENTED review with text is a
    # review, so it answers the marker and H3 becomes terminal.
    review = {
        "login": rf.CODEX_LOGIN,
        "commit_id": H3,
        "state": "COMMENTED",
        "submitted_at": _at(5),
        "body": "Reviewed; no further findings.",
        "top_level": [],
    }
    reviews = (_finding(H1, _at(0)), _finding(H2, _at(1)), _clean(H3, _at(2)), review)
    comments = (_marker(H3, _at(3)),)
    got = _ev(head=H3, reviews=reviews, comments=comments, files=(GATE_FILE,))
    assert got["terminal_head"] == H3


def test_an_edited_comment_dates_its_marker_by_the_edit():
    # A comment created at hour 1 is edited at hour 5 to carry a marker for H3,
    # which Codex reviewed at hour 4. Dated by creation the marker would look
    # answered; dated by the edit it is not, so the round-2 head stays terminal.
    reviews = (_finding(H1, _at(0)), _finding(H2, _at(2)), _clean(H3, _at(4)))
    edited = {**_marker(H3, _at(1)), "updated_at": _at(5)}
    got = _ev(head=H3, reviews=reviews, comments=(edited,), files=(GATE_FILE,))
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


def test_the_report_row_judges_the_rounds_rows_own_read(monkeypatch, capsys):
    # The rounds row's read comes back unreadable. The terminal-head row must block
    # on that same read, never pass on a second one (the seam would say below).
    calls = []

    def fake(*_a, **_k):
        calls.append(1)
        return {"status": "unknown", "errors": ["graphql_unreadable"]}

    ch._report_env(monkeypatch, scheduled=ch._scheduled_marker(ch.HEAD))
    monkeypatch.delenv("_TEST_ROUNDS_ROW")
    monkeypatch.setattr(G._review_budget, "evaluate_pr", fake)
    rc = G.check_pr_report("100", repo=ch.REPO)
    out = capsys.readouterr().out
    assert "rounds         : unreadable" in out
    row = next(ln for ln in out.splitlines() if ln.startswith("terminal-head"))
    assert row.startswith("terminal-head  : BLOCK — the review budget is unreadable"), row
    assert len(calls) == 1
    assert rc == 1


def test_the_report_row_is_ok_below_terminal(monkeypatch, capsys):
    ch._report_env(monkeypatch, scheduled=ch._scheduled_marker(ch.HEAD))
    rc = G.check_pr_report("100", repo=ch.REPO)
    out = capsys.readouterr().out
    row = next(ln for ln in out.splitlines() if ln.startswith("terminal-head"))
    assert row == "terminal-head  : ok (below terminal)"
    assert rc == 0, out


# -- the pin (owner ruling 3f6ad6fb, F-B2): an owner's "merge at head X" ----------


def _pin(
    head: str, when: str = _at(9), *, login: str | None = "owner", assoc: str | None = "OWNER"
) -> dict:
    return {
        "login": login,
        "type": "User" if login else None,
        "author_association": assoc,
        "body": f"Owner ruled: merge at this head.\n<!-- genesis-pin head={head} -->",
        "created_at": when,
    }


def test_no_pin_reads_as_no_pin():
    got = _ev(head=H5)
    assert got["pin_heads"] == [] and got["pin_error"] is None


def test_a_pin_comment_is_read_lowercased():
    got = _ev(head=H5, comments=(_pin("A" * 40),), commits=(H1, "a" * 40, H5))
    assert got["pin_heads"] == ["a" * 40] and got["pin_error"] is None


@pytest.mark.parametrize("assoc", ["OWNER", "MEMBER", "COLLABORATOR"])
def test_a_maintainers_pin_binds(assoc):
    got = _ev(head=H5, comments=(_pin(H3, login="maintainer", assoc=assoc),))
    assert got["pin_heads"] == [H3] and got["pin_error"] is None
    assert G._check_past_terminal("7", "o/r", H5, budget=got)[0] is True


@pytest.mark.parametrize("assoc", ["NONE", "CONTRIBUTOR", "FIRST_TIME_CONTRIBUTOR", None])
def test_a_strangers_pin_is_ignored(assoc):
    # On a public repo anyone can comment: a stranger's pin must not bind or block.
    got = _ev(head=H5, comments=(_pin(H3, login="stranger", assoc=assoc),))
    assert got["pin_heads"] == [] and got["pin_error"] is None
    assert G._check_past_terminal("7", "o/r", H5, budget=got)[0] is False


def test_a_deleted_authors_pin_is_ignored():
    got = _ev(head=H5, comments=(_pin(H3, login=None, assoc="OWNER"),))
    assert got["pin_heads"] == [] and got["pin_error"] is None


def test_a_strangers_disagreeing_or_stray_pin_creates_no_error():
    comments = (
        _pin(H3, _at(8)),
        _pin(H2, _at(9), login="stranger", assoc="NONE"),
        _pin("9" * 40, _at(10), login="stranger", assoc="CONTRIBUTOR"),
    )
    got = _ev(head=H5, comments=comments)
    assert got["pin_heads"] == [H3] and got["pin_error"] is None
    assert G._check_past_terminal("7", "o/r", H3, budget=got)[0] is False


def test_a_pin_outside_the_prs_commits_is_an_error():
    got = _ev(head=H5, comments=(_pin("9" * 40),))
    assert got["status"] == "ok"
    assert got["pin_error"] == "pin_not_in_pr"


def test_two_pins_naming_different_heads_disagree():
    # The newest pin does NOT win (F-B2): every live pin must hold.
    got = _ev(head=H5, comments=(_pin(H2, _at(8)), _pin(H3, _at(9))))
    assert got["pin_heads"] == [H2, H3]
    assert got["pin_error"] == "pins_disagree"


def test_two_pins_naming_one_head_agree():
    got = _ev(head=H5, comments=(_pin(H3, _at(8)), _pin(H3, _at(9))))
    assert got["pin_heads"] == [H3] and got["pin_error"] is None


def test_unknown_evidence_carries_a_blocking_pin_error():
    got = _ev(head="not-a-sha")
    assert got["pin_heads"] == [] and got["pin_error"] == "evidence_unknown"


def _says(body: str, *, assoc: str | None = "OWNER", when=None, edited=None) -> dict:
    """A comment with exactly ``body``, from a maintainer unless ``assoc`` says otherwise."""
    when = when or _at(9)
    return {
        "login": "owner" if assoc else "stranger",
        "type": "User",
        "author_association": assoc or "NONE",
        "body": body,
        "created_at": when,
        "updated_at": edited or when,
    }


@pytest.mark.parametrize(
    "marker",
    [
        pytest.param(f"<!-- genesis-pin head={H3[:7]} -->", id="short-sha"),
        pytest.param(f'<!-- genesis-pin head="{H3}" -->', id="quoted-sha"),
        pytest.param(f"<!-- genesis-pin head={H3} by=owner -->", id="extra-attribute"),
        pytest.param(f"<!-- genesis-pin {H3} -->", id="no-head-key"),
        pytest.param("<!--\ngenesis-pin head=<sha>\n-->", id="placeholder-multiline"),
    ],
)
def test_a_maintainers_malformed_pin_blocks_never_binds_nothing(marker):
    got = _ev(head=H5, comments=(_says(f"Ruling: merge here.\n{marker}"),))
    assert got["pin_error"] == "malformed_pin", got["pin_heads"]
    blocked, msg = G._check_past_terminal("7", "o/r", H5, budget=got)
    assert blocked is True
    assert "malformed_pin" in msg and "<!-- genesis-pin head=<full 40-hex sha> -->" in msg


def test_a_strangers_malformed_pin_is_ignored():
    got = _ev(head=H5, comments=(_says(f"<!-- genesis-pin head={H3[:7]} -->", assoc=None),))
    assert got["pin_heads"] == [] and got["pin_error"] is None


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(f"> <!-- genesis-pin head={H3} -->", id="quote-line"),
        pytest.param(f"Example:\n  >> <!-- genesis-pin head={H2} -->", id="nested-quote"),
        pytest.param(f"```\n<!-- genesis-pin head={H3} -->\n```", id="backtick-fence"),
        pytest.param(f"~~~md\n<!-- genesis-pin head={H2} -->\n~~~", id="tilde-fence"),
        pytest.param(f"To pin, post `<!-- genesis-pin head={H3} -->`.", id="inline-code"),
        pytest.param("To pin, post `<!-- genesis-pin head=<sha> -->`.", id="inline-placeholder"),
        pytest.param(f"```\n<!-- genesis-pin head={H3[:7]} -->\n```", id="malformed-in-fence"),
        pytest.param(f"> <!-- genesis-pin head={H3[:7]} -->", id="malformed-in-quote"),
    ],
)
def test_a_quoted_or_code_pin_is_ignored(body):
    # The agent posts from the maintainer account: an example it quotes must not bind.
    got = _ev(head=H5, comments=(_says(body),))
    assert got["pin_heads"] == [] and got["pin_error"] is None


def test_a_pin_after_a_closed_fence_still_counts():
    body = f"```\nexample\n```\n<!-- genesis-pin head={H3} -->"
    got = _ev(head=H5, comments=(_says(body),))
    assert got["pin_heads"] == [H3] and got["pin_error"] is None


def test_a_stray_backtick_never_swallows_a_pin_in_another_paragraph():
    body = f"it`s ruled.\n\n<!-- genesis-pin head={H3} -->\n\nsee `x`"
    got = _ev(head=H5, comments=(_says(body),))
    assert got["pin_heads"] == [H3] and got["pin_error"] is None


def test_a_pin_added_by_editing_a_comment_counts():
    got = _ev(
        head=H5, comments=(_says(f"<!-- genesis-pin head={H3} -->", when=_at(1), edited=_at(9)),)
    )
    assert got["pin_heads"] == [H3] and got["pin_error"] is None


def test_two_identical_markers_in_one_comment_are_one_pin():
    marker = f"<!-- genesis-pin head={H3} -->"
    got = _ev(head=H5, comments=(_says(f"{marker}\n{marker}"),))
    assert got["pin_heads"] == [H3] and got["pin_error"] is None


def test_two_different_markers_in_one_comment_disagree():
    body = f"<!-- genesis-pin head={H2} -->\n<!-- genesis-pin head={H3} -->"
    got = _ev(head=H5, comments=(_says(body),))
    assert got["pin_error"] == "pins_disagree"


def test_a_pin_landing_between_the_two_reads_is_unknown(monkeypatch):
    """The double read compares the pin fields too: a pin posted mid-evaluation
    is a policy change, never silently dropped."""
    from tests.test_hooks import test_review_budget as trb

    trb._no_seams(monkeypatch)
    late = {
        "body": f"<!-- genesis-pin head={trb.H3} -->",
        "authorAssociation": "OWNER",
        "author": {"login": "owner", "__typename": "User"},
    }
    base = trb._graphql_server(files=trb._FILES, commits=trb._COMMITS)
    grown = trb._graphql_server(comments=[late], files=trb._FILES, commits=trb._COMMITS)
    alone = rb.evaluate_pr("owner/repo", 7, runner=grown, external_identity_templates=())
    assert alone["status"] == "ok" and alone["pin_heads"] == [trb.H3], alone
    calls = []

    def serve(argv, *, timeout):
        calls.append(argv)
        return (base if len(calls) == 1 else grown)(argv, timeout=timeout)

    got = rb.evaluate_pr("owner/repo", 7, runner=serve, external_identity_templates=())
    assert got["status"] == "unknown"
    assert "evidence_changed_during_evaluation" in got["errors"], got


@pytest.mark.parametrize("assoc,counts", [("COLLABORATOR", True), ("NONE", False)])
def test_the_graphql_read_carries_the_author_association(monkeypatch, assoc, counts):
    from tests.test_hooks import test_review_budget as trb

    trb._no_seams(monkeypatch)
    node = {
        "body": f"<!-- genesis-pin head={trb.H3} -->",
        "authorAssociation": assoc,
        "author": {"login": "someone", "__typename": "User"},
    }
    run = trb._graphql_server(comments=[node], files=trb._FILES, commits=trb._COMMITS)
    got = rb.evaluate_pr("owner/repo", 7, runner=run, external_identity_templates=())
    assert got["status"] == "ok", got
    assert got["pin_heads"] == ([trb.H3] if counts else [])
    assert any("authorAssociation" in a for a in run.seen[0])


X, Y = N1, N2  # two PR heads


def _below(current: str = Y, *, pins=(), pin_error=None) -> dict:
    return {
        "status": "ok",
        "terminal_state": "below",
        "terminal_head": None,
        "current_head": current,
        "pin_heads": list(pins),
        "pin_error": pin_error,
    }


def test_below_terminal_a_pin_lets_its_head_pass():
    blocked, msg = G._check_past_terminal("7", "o/r", X, budget=_below(X, pins=[X]))
    assert blocked is False, msg
    assert f"pinned head {X[:12]}" in msg


def test_below_terminal_a_pin_blocks_any_other_head():
    blocked, msg = G._check_past_terminal("7", "o/r", Y, budget=_below(Y, pins=[X]))
    assert blocked is True
    assert f"pinned head {X}" in msg
    assert "! gh pr merge 7" in msg


def test_an_unbound_merge_on_a_pinned_pr_blocks():
    blocked, _ = G._check_past_terminal("7", "o/r", "", budget=_below(X, pins=[X]))
    assert blocked is True


def test_a_pin_head_comparison_ignores_case():
    blocked, msg = G._check_past_terminal("7", "o/r", X.upper(), budget=_below(X, pins=[X]))
    assert blocked is False, msg


@pytest.mark.parametrize("error", ["pin_not_in_pr", "pins_disagree", "evidence_unknown"])
def test_a_pin_error_blocks_even_at_a_pinned_head(error):
    budget = _below(X, pins=[X], pin_error=error)
    blocked, msg = G._check_past_terminal("7", "o/r", X, budget=budget)
    assert blocked is True
    assert error in msg and "! gh pr merge 7" in msg


def test_a_malformed_pin_record_blocks():
    budget = {**_below(X), "pin_heads": X}  # a string, not a list
    assert G._check_past_terminal("7", "o/r", X, budget=budget)[0] is True


def test_past_terminal_a_pin_never_loosens_the_terminal_rule():
    # Pinned at N1 (not the terminal head T): merging at the pin still blocks...
    budget = {**_past(N1), "pin_heads": [N1], "pin_error": None}
    blocked, msg = G._check_past_terminal("7", "o/r", N1, budget=budget)
    assert blocked is True and f"terminal head {T}" in msg
    # ...and merging at T blocks too, since the pin names N1.
    blocked, msg = G._check_past_terminal("7", "o/r", T, budget={**budget, "current_head": T})
    assert blocked is True and f"pinned head {N1}" in msg


def test_past_terminal_a_pin_at_the_terminal_head_passes():
    budget = {**_past(T), "pin_heads": [T], "pin_error": None}
    blocked, msg = G._check_past_terminal("7", "o/r", T, budget=budget)
    assert blocked is False, msg
    assert "pinned" in msg


def test_past_terminal_a_pin_is_strict_over_empty_commits():
    # The empty-commit allowance is the terminal rule's; a pin names one sha.
    budget = {**_past(E1), "pin_heads": [T], "pin_error": None}
    assert G._check_past_terminal("7", "o/r", E1, budget=budget)[0] is True


def test_the_evaluator_pin_drives_the_check_end_to_end():
    pinned = _ev(head=H5, comments=(_pin(H3),))
    assert G._check_past_terminal("7", "o/r", H3, budget=pinned)[0] is False
    assert G._check_past_terminal("7", "o/r", H5, budget=pinned)[0] is True
    stray = _ev(head=H5, comments=(_pin("9" * 40),))
    assert G._check_past_terminal("7", "o/r", H5, budget=stray)[0] is True


def test_the_merge_arm_blocks_a_merge_off_the_pinned_head(monkeypatch, capsys):
    _seam(monkeypatch, _below(ch.HEAD, pins=[T]))
    rc = ch._run(monkeypatch, ch._merge_cmd())
    assert rc == 2
    assert f"pinned head {T}" in capsys.readouterr().err


def test_the_merge_arm_allows_a_merge_at_the_pinned_head(monkeypatch):
    _seam(monkeypatch, _below(ch.HEAD, pins=[ch.HEAD]))
    assert ch._run(monkeypatch, ch._merge_cmd()) == 0


def test_the_report_row_shows_a_pin_it_blocks_on(monkeypatch, capsys):
    ch._report_env(monkeypatch, scheduled=ch._scheduled_marker(ch.HEAD))
    _seam(monkeypatch, _below(ch.HEAD, pins=[T]))
    rc = G.check_pr_report("100", repo=ch.REPO)
    out = capsys.readouterr().out
    row = next(ln for ln in out.splitlines() if ln.startswith("terminal-head"))
    assert row.startswith("terminal-head  : BLOCK — ") and f"pinned head {T}" in row, row
    assert rc == 1


def test_the_report_row_shows_a_pin_it_passes(monkeypatch, capsys):
    ch._report_env(monkeypatch, scheduled=ch._scheduled_marker(ch.HEAD))
    _seam(monkeypatch, _below(ch.HEAD, pins=[ch.HEAD]))
    rc = G.check_pr_report("100", repo=ch.REPO)
    out = capsys.readouterr().out
    row = next(ln for ln in out.splitlines() if ln.startswith("terminal-head"))
    want = f"terminal-head  : ok (below terminal, merging at the pinned head {ch.HEAD[:12]})"
    assert row == want, row
    assert rc == 0, out
