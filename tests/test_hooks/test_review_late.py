"""Late findings the round reflection owes (``review_late``), read from the
facts ``review_budget`` states: ``late`` per finding and ``opened_at`` per
round.

Every budget here is built by ``review_budget.evaluate_evidence`` from reviews
and a commit graph, never written by hand, so a test can only pass on a shape
the producer really emits. A few tests then corrupt or delete one producer
field, to stand for an older or broken budget. Coverage tests run in real
temporary git repositories with pinned dates, because emptiness, ancestry and
the fix-time rule are git behaviours.
"""

from __future__ import annotations

import itertools
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "scripts"))

import review_budget as rb  # noqa: E402
import review_findings as rf  # noqa: E402
import review_late as rl  # noqa: E402
import review_reflection as rr  # noqa: E402

T0 = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
CUT = (T0 - timedelta(days=1)).isoformat()
LEGACY_CUT = datetime.fromisoformat(rb.ROUND_RULE_CUTOVER_ISO)
CODEX = rf.CODEX_LOGIN
DEVIN = "devin-ai-integration[bot]"
GATE_FILE = "scripts/hooks/git_push_guard.py"
P2 = "**<sub><sub>![P2 Badge](https://img.shields.io/badge/P2-yellow)</sub></sub>  Off by one**"
H1, H2, H3, H4, H5 = ("1" * 40, "2" * 40, "3" * 40, "4" * 40, "5" * 40)
GONE = "7" * 40  # a commit a force-push removed from the PR
_IDS = itertools.count(1000)


def _at(minutes: float) -> str:
    return (T0 + timedelta(minutes=minutes)).isoformat()


def _rv(head, when, *ids, login=CODEX, state="COMMENTED", keyed=True):
    """A finding review (one P2 per comment id) or, with no ids, a clean one."""
    review = {
        "login": login,
        "commit_id": head,
        "state": state,
        "submitted_at": when,
        "body": "" if ids else "Looks good.",
        "top_level": [P2] * len(ids),
    }
    if keyed:
        review.update(id=next(_IDS), top_level_ids=list(ids))
    return review


def _dag(*heads, extra=None):
    """A linear PR history over ``heads``; ``extra`` maps a sha to its parents."""
    extra = extra or {}
    return [
        {"sha": h, "parents": extra.get(h, [heads[i - 1]] if i else [])}
        for i, h in enumerate(heads)
    ]


def _produce(commits, reviews, *, current, gate=False):
    budget = rb.evaluate_evidence(
        current_head=current,
        commit_heads=commits,
        reviews=reviews,
        issue_comments=(),
        changed_files=(GATE_FILE,) if gate else ("src/x.py",),
        cutover=rb.ROUND_RULE_CUTOVER_ISO,
    )
    assert budget["status"] == "ok", budget
    return budget


def _owed(budget, *, fix=None, covered=(), late_covered=(), cutover=CUT):
    fix_time = (lambda head: fix) if fix is not ... else None  # noqa: E731
    return rr.owed_state(
        budget, covered, now=T0, cutover=cutover, fix_time=fix_time, late_covered=late_covered
    )


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


# -- the cutover None path is main's ----------------------------------------------


def test_the_shipped_cutover_is_the_gates_go_live_moment():
    """Ruling 50cc9dec: nothing new is owed before the commit gate enforces, so the
    cutover is the moment it started to: a real, zoned time no later than now and
    no earlier than the enforcement change itself."""
    cut = datetime.fromisoformat(rr.REFLECTION_CUTOVER_ISO)
    assert cut.tzinfo is not None
    assert datetime(2026, 10, 21, tzinfo=UTC) <= cut <= datetime.now(UTC)


def test_with_no_cutover_review_late_is_never_imported():
    """S1: with the cutover None, owed_state and status run main's code; the
    late reader is not even loaded."""
    code = (
        "import sys; sys.path.insert(0, 'scripts')\n"
        "from datetime import UTC, datetime\n"
        "import review_budget as rb, review_reflection as rr\n"
        "b = rb.evaluate_evidence(current_head='2'*40, commit_heads=[{'sha':'1'*40,'parents':[]},"
        "{'sha':'2'*40,'parents':['1'*40]}], reviews=[], issue_comments=(), changed_files=())\n"
        "rr.owed_state(b, [], now=datetime.now(UTC))\n"
        "print('review_late' in sys.modules)\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], cwd=_ROOT, capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "False"


def test_with_no_cutover_nothing_late_is_owed():
    budget = _produce(
        _dag(H1, H2),
        [_rv(H1, _at(0), 1), _rv(H2, _at(30)), _rv(H1, _at(90), 9)],
        current=H2,
    )
    state = rr.owed_state(budget, [], now=T0)
    assert state["owed"] == [] and state["late_keys"] == [] and state["cutover"] is None
    assert state["settled"] is False


# -- what is owed -------------------------------------------------------------------


def test_a_late_review_on_a_head_that_already_is_a_round_is_owed():
    """Codex c4273454984: a late review of a head that already opened a round
    is ALSO listed in rounds[].reviews. The reader keys on the producer's
    ``late``, never on that membership."""
    budget = _produce(
        _dag(H1, H2),
        [_rv(H1, _at(0), 1), _rv(H2, _at(30)), _rv(H1, _at(90), 9)],
        current=H2,
    )
    late_one = [f for f in budget["findings"] if f["keys"] == ["c9"]][0]
    assert late_one["late"] is True and late_one["round_head"] == H1
    assert ["c9"] in [r["finding_keys"] for r in budget["rounds"][0]["reviews"]]
    # Owed even though no fix exists yet: the producer said it was late.
    state = _owed(budget, fix=None)
    assert state["owed"] == ["c9"] and state["late_keys"] == ["c9"]


def test_a_round_opening_review_is_owed_only_once_a_fix_existed():
    """Owner ruling e70d4934: a review that opened (or joined) its round was
    the open round's while it was open; outside it, it is owed only when it
    arrived after a fix to its head."""
    budget = _produce(_dag(H1, H2), [_rv(H1, _at(0), 1), _rv(H1, _at(90), 9)], current=H2)
    assert all(f["late"] is False for f in budget["findings"])
    assert _owed(budget, fix=T0 + timedelta(minutes=60))["owed"] == ["c9"]
    assert _owed(budget, fix=None)["owed"] == []
    # A fix time that is needed but not given: unknown, never "nothing".
    assert _owed(budget, fix=...)["owed"] is None


def test_findings_before_the_cutover_are_not_owed():
    budget = _produce(
        _dag(H1, H2),
        [_rv(H1, _at(0), 1), _rv(H2, _at(30)), _rv(H1, _at(90), 9)],
        current=H2,
    )
    assert _owed(budget, cutover=_at(60))["owed"] == ["c9"]
    assert _owed(budget, cutover=_at(100))["owed"] == []


def test_an_unreadable_cutover_is_unknown():
    budget = _produce(_dag(H1, H2), [_rv(H1, _at(0), 1)], current=H2)
    state = _owed(budget, cutover="soon")
    assert state["owed"] is None and state["late_keys"] is None and state["settled"] is False


def test_an_unknown_budget_leaves_late_keys_unknown():
    """S5: with a cutover set, late_keys starts unknown and stays unknown when
    the budget is; a reader never sees a known empty next to owed None."""
    state = _owed(rb._unknown("test"))
    assert state["owed"] is None and state["late_keys"] is None
    # The reader on its own says the same, whatever its caller checks first.
    assert rl.late_findings(rb._unknown("test"), CUT) is None
    assert rl.late_owed(rb._unknown("test"), (), cutover=CUT) == (None, None)


def test_a_settled_state_with_a_cutover():
    """N7: no settle window applies to late reviews (each is owed as it lands),
    so with no round open the state is settled once the debt is known."""
    budget = _produce(
        _dag(H1, H2),
        [_rv(H1, _at(0), 1), _rv(H2, _at(30)), _rv(H1, _at(90), 9)],
        current=H2,
    )
    assert _owed(budget)["settled"] is True
    assert _owed(budget, late_covered=["c9"])["owed"] == []
    assert _owed(budget, late_covered=["c9"])["settled"] is True


def test_open_round_coverage_never_clears_a_late_key():
    """Codex c4266451771: a reflection judged under the open round that also
    names a late key must not clear it; only a credit under its own round
    does."""
    budget = _produce(
        _dag(H1, H2, H3),
        [_rv(H1, _at(0), 1), _rv(H2, _at(30)), _rv(H1, _at(90), 9), _rv(H3, _at(100), 3)],
        current=H3,
    )
    assert budget["round_state"] == "open" and budget["open_keys"] == ["c3"]
    assert _owed(budget, covered=["c3", "c9"])["owed"] == ["c9"]
    assert _owed(budget, covered=["c3"], late_covered=["c9"])["owed"] == []


def test_the_open_rounds_own_keys_are_not_counted_twice():
    budget = _produce(_dag(H1), [_rv(H1, _at(0), 1), _rv(H1, _at(90), 9)], current=H1)
    assert budget["round_state"] == "open"
    state = _owed(budget, fix=T0)
    assert state["late_keys"] == [] and state["owed"] == ["c1", "c9"]
    # A late key also in open_keys cannot happen (#3347), but the kept rule
    # would still count it once, under the open round.
    budget["findings"][1]["late"] = True
    assert _owed(budget, fix=None)["owed"] == ["c1", "c9"]


@pytest.mark.parametrize("value", ["missing", "yes", None])
def test_a_late_flag_that_is_not_a_bool_is_unknown(value):
    """N5: an older budget without ``late`` (or a corrupt one) is unknown,
    never read as False."""
    budget = _produce(
        _dag(H1, H2),
        [_rv(H1, _at(0), 1), _rv(H2, _at(30)), _rv(H1, _at(90), 9)],
        current=H2,
    )
    if value == "missing":
        del budget["findings"][-1]["late"]
    else:
        budget["findings"][-1]["late"] = value
    assert _owed(budget, fix=None)["owed"] is None


def test_a_finding_off_the_graph_is_dropped_before_its_keys_are_read():
    """Codex c4273455004 and owner ruling a3a676b6: a force-push drops late
    debt. A force-pushed head's review with no derivable key makes the
    producer's late_state unknown, but owes nothing here (S4: late_state is
    deliberately not read)."""
    budget = _produce(
        _dag(H1, H2),
        [_rv(GONE, _at(0), 5, keyed=False), _rv(H1, _at(10), 1)],
        current=H2,
    )
    assert budget["late_state"] == "unknown"
    assert [f["keys"] for f in budget["findings"] if f["head"] == GONE] == [[None]]
    assert _owed(budget, fix=None)["owed"] == []
    # Even when every finding would count as after its fix, the force-pushed
    # head is dropped first: its key is never read, and no fix time is asked
    # for a commit local git may not have.
    asked = []

    def fix_time(head):
        asked.append(head)
        return T0 - timedelta(minutes=1)

    state = rr.owed_state(budget, [], now=T0, cutover=CUT, fix_time=fix_time)
    assert state["owed"] == ["c1"] and GONE not in asked


def test_an_unkeyable_finding_that_owes_nothing_changes_nothing():
    """A round-opening review that arrived before any fix owes nothing, so a
    key it could not derive must not make owed unknown, and the open round's
    debt stays known."""
    budget = _produce(
        _dag(H1, H2, H3),
        [_rv(H1, _at(0), 1, keyed=False), _rv(H3, _at(100), 3)],
        current=H3,
    )
    assert budget["findings"][0]["keys"] == [None] and budget["findings"][0]["late"] is False
    assert _owed(budget, fix=T0 + timedelta(minutes=60))["owed"] == ["c3"]
    # Owed (after its fix) it is unknown again: a key no one could clear.
    assert _owed(budget, fix=T0 - timedelta(minutes=1))["owed"] is None


def test_an_unkeyable_in_graph_finding_is_unknown():
    budget = _produce(
        _dag(H1, H2),
        [_rv(H1, _at(0), 1), _rv(H2, _at(30)), _rv(H1, _at(90), 9, keyed=False)],
        current=H2,
    )
    assert _owed(budget, fix=None)["owed"] is None


# -- a round's context ----------------------------------------------------------------


def test_rounds_are_numbered_by_when_they_opened_not_by_list_order():
    base, left, right = H1, H2, H3
    commits = _dag(base, left, right, extra={right: [base]})
    budget = _produce(commits, [_rv(right, _at(0), 2), _rv(left, _at(10), 1)], current=left)
    assert [r["head"] for r in budget["rounds"]] == [left, right]  # list order: commits
    later = T0 + timedelta(hours=1)
    assert rl.round_context(budget, right, later) == rl.RoundContext(1, _at(0), ())
    assert rl.round_context(budget, left, later) == rl.RoundContext(2, _at(10), (right,))
    # As of a time before it opened, a round has no context yet.
    assert rl.round_context(budget, left, T0 + timedelta(minutes=5)) is None
    assert rl.round_context(budget, H5, later) is None


def test_a_non_primary_pre_cutover_review_is_no_legacy_opening():
    """Codex c4273455013: only the primary's pre-cutover evidence makes a
    legacy round. H2's earlier Devin review must not move its round ahead of
    the legacy round H1."""
    old = LEGACY_CUT - timedelta(days=1)
    reviews = [
        _rv(H2, (old - timedelta(days=1)).isoformat(), 4, login=DEVIN),
        _rv(H1, old.isoformat()),  # the primary, before the cutover: legacy
        _rv(H2, _at(0), 2),
    ]
    budget = _produce(_dag(H1, H2), reviews, current=H2)
    later = T0 + timedelta(hours=1)
    assert rl.round_context(budget, H1, later) == rl.RoundContext(1, old.isoformat(), ())
    assert rl.round_context(budget, H2, later) == rl.RoundContext(2, _at(0), (H1,))


def test_an_untimed_legacy_round_sorts_before_every_post_cutover_round():
    """N1: a legacy round with opened_at None (pending primary evidence) opened
    before the cutover, so a later round's number is still known. Only a key
    of the untimed round itself is unknown."""
    old = (LEGACY_CUT - timedelta(days=1)).isoformat()
    reviews = [
        {**_rv(H1, None, state="PENDING"), "body": "x"},
        _rv(H1, old),
        _rv(H2, _at(0), 2),
        _rv(H3, _at(10)),
        _rv(H2, _at(20), 9),  # late: H3 was reviewed first
    ]
    budget = _produce(_dag(H1, H2, H3), reviews, current=H3)
    assert budget["rounds"][0]["opened_at"] is None
    later = T0 + timedelta(hours=1)
    assert rl.round_context(budget, H2, later) == rl.RoundContext(2, _at(0), (H1,))
    assert rl.round_context(budget, H1, later) is None
    assert _owed(budget, fix=None)["owed"] == ["c9"]
    # A late key of the untimed round itself cannot be placed: unknown.
    budget2 = _produce(_dag(H1, H2, H3), [*reviews, _rv(H1, _at(30), 8)], current=H3)
    assert _owed(budget2, fix=None)["owed"] is None


def test_an_untimed_legacy_round_is_unordered_against_a_timed_legacy_round():
    """N1: both opened before the cutover, and one has no time, so neither's
    position is known; a key of the timed one is unknown too."""
    old = (LEGACY_CUT - timedelta(days=1)).isoformat()
    reviews = [
        {**_rv(H1, None, state="PENDING"), "body": "x"},
        _rv(H1, old),
        _rv(H2, old),
        _rv(H3, _at(10)),
        _rv(H2, _at(20), 9),  # late: H3 was reviewed first
    ]
    budget = _produce(_dag(H1, H2, H3), reviews, current=H3)
    assert [r["opened_at"] is None for r in budget["rounds"]] == [True, False]
    assert rl.round_context(budget, H2, T0 + timedelta(hours=1)) is None
    assert _owed(budget, fix=None)["owed"] is None


def test_a_tie_involving_the_keys_own_round_is_unknown():
    """N2: two rounds opened at the same instant are unordered; a key of
    either is unknown, never sorted by sha."""
    tied = _produce(
        _dag(H1, H2, H3),
        [_rv(H1, _at(0), 1), _rv(H2, _at(0), 2), _rv(H3, _at(40)), _rv(H1, _at(50), 9)],
        current=H3,
    )
    assert _owed(tied, fix=None)["owed"] is None
    apart = _produce(
        _dag(H1, H2, H3),
        [_rv(H1, _at(0), 1), _rv(H2, _at(1), 2), _rv(H3, _at(40)), _rv(H1, _at(50), 9)],
        current=H3,
    )
    assert _owed(apart, fix=None)["owed"] == ["c9"]


def test_a_budget_without_opened_at_is_unknown():
    budget = _produce(
        _dag(H1, H2),
        [_rv(H1, _at(0), 1), _rv(H2, _at(30)), _rv(H1, _at(90), 9)],
        current=H2,
    )
    del budget["rounds"][0]["opened_at"]
    assert _owed(budget, fix=None)["owed"] is None


def test_a_late_review_with_no_earlier_round_is_round_zero():
    """N4: the reader finds no older round exactly when the producer's
    round_of did not."""
    budget = _produce(
        _dag(H1, H2), [_rv(H1, _at(0)), _rv(H2, _at(10)), _rv(H1, _at(20), 9)], current=H2
    )
    item = budget["findings"][0]
    assert item["late"] is True and item["round_head"] is None and budget["rounds"] == []
    _, ancestors = rl._ancestry(budget)
    contexts = rl._contexts(budget, {**item, "at": T0}, T0, ancestors)
    assert contexts == [rl.RoundContext(0, T0.isoformat(), ())]


def test_a_late_review_below_a_merge_of_two_rounds_answers_under_both():
    base, left, right, merge, tip = H1, H2, H3, H4, H5
    commits = _dag(base, left, right, merge, tip, extra={right: [base], merge: [left, right]})
    reviews = [
        _rv(left, _at(0), 1),
        _rv(right, _at(5), 2),
        _rv(tip, _at(20)),
        _rv(merge, _at(30), 9),
    ]
    budget = _produce(commits, reviews, current=tip)
    item = [f for f in budget["findings"] if f["keys"] == ["c9"]][0]
    assert item["late"] is True and item["round_head"] is None
    _, ancestors = rl._ancestry(budget)
    contexts = rl._contexts(budget, {**item, "at": T0}, T0 + timedelta(hours=1), ancestors)
    assert [c.round_number for c in contexts] == [1, 2]


def test_unknown_ancestry_for_a_needed_placement_is_unknown():
    commits = _dag(H1, H2, H3, extra={H2: None})
    budget = _produce(commits, [_rv(H3, _at(10)), _rv(H2, _at(20), 9)], current=H3)
    item = budget["findings"][0]
    assert item["late"] is True and item["round_head"] is None
    assert _owed(budget, fix=None)["owed"] is None


# -- coverage, in a real repository --------------------------------------------------


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


def _graph(repo: Path, base: str, tip: str) -> list[dict]:
    """The PR's commits from ``base`` to ``tip``, as ``evaluate_pr`` reads them."""
    rows = _git(repo, "log", "--reverse", "--format=%H %P", f"{base}..{tip}").split("\n")
    return [{"sha": r.split()[0], "parents": r.split()[1:]} for r in rows if r.strip()]


@pytest.fixture
def pr(tmp_path):
    """A base commit, then round head H (T0-10m) and its fix F (T0+60m)."""
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


def _late_budget(path, base, tip, reviews, *, gate=False):
    return _produce(_graph(path, base, tip), reviews, current=tip, gate=gate)


def _credit(path, budget, *, gate=False, acceptance=None):
    reader = rl.fix_time_reader(str(path), str(budget["current_head"]))
    late = rl.late_findings(budget, CUT, reader)
    assert late is not None
    return rl.covered_late_keys(str(path), budget, late, gate_lane=gate, acceptance=acceptance)


def test_a_review_after_the_fix_is_answered_at_the_fix(pr, tmp_path):
    """The owner's scenario: a review of round head H lands after the fix F
    exists; a reflection at F, after the review, answers it under H's round.
    The review before the fix was the open round's, and is not owed."""
    path, base, head, fix = pr
    budget = _late_budget(path, base, fix, [_rv(head, _at(0), 1), _rv(head, _at(90), 9)])
    reader = rl.fix_time_reader(str(path), fix)
    assert [i["key"] for i in rl.late_findings(budget, CUT, reader)] == ["c9"]
    assert _credit(path, budget) == set()
    _reflect(path, tmp_path, _block(["c9"], fix), T0 + timedelta(minutes=120))
    assert _credit(path, budget) == {"c9"}


def test_a_reflection_made_after_a_fix_cannot_clear_a_review_from_before_it(pr, tmp_path):
    """A late review that arrived before the fix was owed before the fix: a
    reflection made after it is a rationalisation. An empty commit E (no fix)
    is what made the review late."""
    path, base, head, _ = pr
    _git(path, "reset", "-q", "--hard", head)
    empty = _reflect(path, tmp_path, "wip\n", T0 + timedelta(minutes=5))
    fix = _change(path, "z\n", T0 + timedelta(minutes=60))
    reviews = [_rv(empty, _at(10)), _rv(head, _at(20), 9)]
    budget = _late_budget(path, base, fix, reviews)
    assert budget["findings"][0]["late"] is True
    _reflect(path, tmp_path, _block(["c9"], fix), T0 + timedelta(minutes=120))
    assert _credit(path, budget) == set()


def test_an_answer_cannot_predate_its_finding(pr, tmp_path):
    path, base, head, fix = pr
    budget = _late_budget(path, base, fix, [_rv(head, _at(0), 1), _rv(head, _at(180), 9)])
    _reflect(path, tmp_path, _block(["c9"], fix), T0 + timedelta(minutes=120))
    assert _credit(path, budget) == set()


def test_the_answer_is_held_to_its_own_rounds_obligations(pr, tmp_path):
    """Gate lane, round 1 owes an audit: a reflection without one answers
    nothing, and the same reflection citing the round's audit does."""
    path, base, head, fix = pr
    reviews = [_rv(head, _at(0), 1), _rv(head, _at(90), 9)]
    budget = _late_budget(path, base, fix, reviews, gate=True)
    _reflect(path, tmp_path, _block(["c9"], fix), T0 + timedelta(minutes=120))
    assert _credit(path, budget, gate=True) == set()
    audit = tmp_path / "audit.txt"
    audit.write_text(
        "Severity ladder: BLOCKER none; SHOULD-FIX scripts/x.py:12 the loop is unbounded; "
        "NOTE scripts/y.py:3 a stale comment. Scope: the whole diff was read. " * 4
    )
    stamp = (T0 + timedelta(minutes=100)).timestamp()
    os.utime(audit, (stamp, stamp))
    text = _block(["c9"], fix, extra=[f"Audit-evidence: {audit} class-audit"])
    _reflect(path, tmp_path, text, T0 + timedelta(minutes=130))
    assert _credit(path, budget, gate=True) == {"c9"}


def _round_zero(path, base, head, fix, *, gate=False):
    """No round at all: H and F are reviewed clean, then a finding lands on
    H, which F's review makes late."""
    reviews = [_rv(head, _at(0)), _rv(fix, _at(70)), _rv(head, _at(90), 9)]
    budget = _late_budget(path, base, fix, reviews, gate=gate)
    assert budget["rounds"] == [] and budget["findings"][0]["round_head"] is None
    return budget


def test_round_zero_owes_no_evidence_and_no_acceptance(pr, tmp_path):
    """Codex c4266451775 and the owner (rev 3): round 0 owes the grammar only,
    even on the gate lane, with an acceptance list the Scope does not cover."""
    path, base, head, fix = pr
    budget = _round_zero(path, base, head, fix, gate=True)
    _reflect(path, tmp_path, _block(["c9"], fix), T0 + timedelta(minutes=120))
    assert _credit(path, budget, gate=True, acceptance=["the cache survives a restart"]) == {"c9"}


def test_round_zero_still_refuses_an_escalating_reflection(pr, tmp_path):
    path, base, head, fix = pr
    budget = _round_zero(path, base, head, fix)
    escalating = _block(["c9"], fix).replace("Escalate: no", "Escalate: yes")
    _reflect(path, tmp_path, escalating, T0 + timedelta(minutes=120))
    assert _credit(path, budget) == set()


def test_unknown_ancestry_credits_nothing(pr, tmp_path):
    path, base, head, fix = pr
    commits = _graph(path, base, fix)
    commits[1]["parents"] = None  # F's parents were not read
    budget = _produce(commits, [_rv(head, _at(0), 1), _rv(head, _at(90), 9)], current=fix)
    _reflect(path, tmp_path, _block(["c9"], fix), T0 + timedelta(minutes=120))
    assert _credit(path, budget) == set()


def test_a_reflection_off_the_current_line_credits_nothing(pr, tmp_path):
    """A force-push replaced F with X: a reflection naming F is not on the
    PR's line any more."""
    path, base, head, fix = pr
    _reflect(path, tmp_path, _block(["c9"], fix), T0 + timedelta(minutes=120))
    _git(path, "checkout", "-q", "-b", "other", head)
    other = _change(path, "w\n", T0 + timedelta(minutes=70))
    _git(path, "checkout", "-q", "feat/x")
    budget = _late_budget(path, base, other, [_rv(head, _at(0), 1), _rv(head, _at(90), 9)])
    late = rl.late_findings(budget, CUT, rl.fix_time_reader(str(path), other))
    assert [i["key"] for i in late] == ["c9"]
    assert rl.covered_late_keys(str(path), budget, late, gate_lane=False, acceptance=None) == set()


def test_a_reflection_after_a_further_fix_credits_nothing(pr, tmp_path):
    path, base, head, fix = pr
    further = _change(path, "after\n", T0 + timedelta(minutes=110))
    budget = _late_budget(path, base, further, [_rv(head, _at(0), 1), _rv(head, _at(90), 9)])
    _git(path, "checkout", "-q", "-b", "late", further)
    _reflect(path, tmp_path, _block(["c9"], fix), T0 + timedelta(minutes=120))
    assert _credit(path, budget) == set()


def test_the_deadline_refuses(pr, tmp_path):
    path, base, head, fix = pr
    budget = _late_budget(path, base, fix, [_rv(head, _at(0), 1), _rv(head, _at(90), 9)])
    _reflect(path, tmp_path, _block(["c9"], fix), T0 + timedelta(minutes=120))
    late = rl.late_findings(budget, CUT, rl.fix_time_reader(str(path), fix))
    with pytest.raises(rr.Refused):
        rl.covered_late_keys(
            str(path), budget, late, gate_lane=False, acceptance=None, deadline=0.0
        )


def test_an_empty_commit_is_no_fix_but_a_merge_is(pr, tmp_path):
    path, base, head, fix = pr
    _git(path, "checkout", "-q", "-b", "empty", head)
    _reflect(path, tmp_path, "not a reflection\n", T0 + timedelta(minutes=30))
    empty_tip = _git(path, "rev-parse", "HEAD")
    assert rl._fix_times(str(path), head, empty_tip) == []
    _git(path, "checkout", "-q", "-b", "merged", head)
    _git(path, "merge", "-q", "--no-ff", "-m", "merge", "empty", when=T0 + timedelta(minutes=40))
    merged = _git(path, "rev-parse", "HEAD")
    assert rl._fix_times(str(path), head, merged) == [T0 + timedelta(minutes=40)]


def test_an_amended_fix_keeps_its_author_time(pr, tmp_path):
    """The fix is amended after the review: its author time stays before the
    review, so the review is still owed and still answerable."""
    path, base, head, fix = pr
    stamp = (T0 + timedelta(minutes=150)).strftime("%Y-%m-%dT%H:%M:%S+0000")
    subprocess.run(
        ["git", "-C", str(path), "commit", "-q", "--amend", "--no-edit"],
        check=True,
        env={**os.environ, "GIT_COMMITTER_DATE": stamp},
    )
    amended = _git(path, "rev-parse", "HEAD")
    budget = _late_budget(path, base, amended, [_rv(head, _at(0), 1), _rv(head, _at(90), 9)])
    reader = rl.fix_time_reader(str(path), amended)
    assert reader(head) == T0 + timedelta(minutes=60)
    assert [i["key"] for i in rl.late_findings(budget, CUT, reader)] == ["c9"]
    _reflect(path, tmp_path, _block(["c9"], amended), T0 + timedelta(minutes=160))
    assert _credit(path, budget) == {"c9"}


def test_the_latest_fix_decides_not_the_first(pr, tmp_path):
    """F, then review c9, then G (its fix) with no reflection, then a
    reflection at G: that is after c9's fix, so it answers nothing."""
    path, base, head, fix = pr
    second = _change(path, "z2\n", T0 + timedelta(minutes=100))
    reviews = [_rv(head, _at(0), 1), _rv(head, _at(90), 9)]
    budget = _late_budget(path, base, second, reviews)
    _reflect(path, tmp_path, _block(["c9"], second), T0 + timedelta(minutes=120))
    assert _credit(path, budget) == set()
    # The control: the same reflection timing at F, before G, answers it.
    _git(path, "checkout", "-q", "-b", "at-f", fix)
    _reflect(path, tmp_path, _block(["c9"], fix), T0 + timedelta(minutes=95))
    assert _credit(path, _late_budget(path, base, fix, reviews)) == {"c9"}


def test_a_review_between_two_fixes_is_owed_from_the_first(pr, tmp_path):
    path, base, head, fix = pr
    second = _change(path, "z2\n", T0 + timedelta(minutes=100))
    budget = _late_budget(path, base, second, [_rv(head, _at(0), 1), _rv(head, _at(80), 9)])
    reader = rl.fix_time_reader(str(path), second)
    assert [i["key"] for i in rl.late_findings(budget, CUT, reader)] == ["c9"]


def test_a_commit_pushed_before_its_review_landed_leaves_the_key_answerable(tmp_path):
    """The architect's blocker on the 2026-10-20 plan: H is pushed and opens a
    round, C is committed and pushed before review c9 of H lands, so c9 is
    owed (it arrived after a fix existed, ruling e70d4934). A reflection naming
    H, made after C, used to be refused for good because C sits between them.
    C predates c9, so it is no fix of it (owner ruling 2026-10-20)."""
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
    pushed = _change(path, "local\n", T0 + timedelta(minutes=5))
    reviews = [_rv(head, _at(0), 1), _rv(head, _at(20), 9)]
    budget = _late_budget(path, base, pushed, reviews)
    reader = rl.fix_time_reader(str(path), pushed)
    assert [i["key"] for i in rl.late_findings(budget, CUT, reader)] == ["c9"]
    _reflect(path, tmp_path, _block(["c9"], head), T0 + timedelta(minutes=30))
    assert _credit(path, budget) == {"c9"}


def test_a_commit_after_the_review_still_refuses_an_answer_naming_the_head(tmp_path):
    """The control: a commit between H and the reflection made after c9 landed
    is a fix of c9, so the reflection naming H is refused."""
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
    pushed = _change(path, "local\n", T0 + timedelta(minutes=5))
    reviews = [_rv(head, _at(0), 1), _rv(head, _at(20), 9)]
    budget = _late_budget(path, base, pushed, reviews)
    _change(path, "fixes c9\n", T0 + timedelta(minutes=25))
    _reflect(path, tmp_path, _block(["c9"], head), T0 + timedelta(minutes=30))
    assert _credit(path, budget) == set()


def _pushed_before_review(tmp_path):
    """H opens a round with c1, C is committed and pushed at +5, review c9 of H
    lands at +20: c9 is owed (e70d4934)."""
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
    pushed = _change(path, "local\n", T0 + timedelta(minutes=5))
    budget = _late_budget(path, base, pushed, [_rv(head, _at(0), 1), _rv(head, _at(20), 9)])
    return path, head, pushed, budget


def test_a_credit_survives_pushing_the_reflection_and_moving_past_it(tmp_path):
    """Round 1 of #3493 (Codex c4326223562, Devin c4326186666): credited while
    the reflection R was ahead of the tip, then R and a commit D after it are
    pushed. The pre-review commit C between H and R is no fix of c9 however far
    the tip has moved, so the answer stands."""
    path, head, _, budget = _pushed_before_review(tmp_path)
    _reflect(path, tmp_path, _block(["c9"], head), T0 + timedelta(minutes=30))
    assert _credit(path, budget) == {"c9"}
    base = _git(path, "rev-list", "--max-parents=0", "HEAD")
    tip = _change(path, "after the answer\n", T0 + timedelta(minutes=40))
    pushed = _late_budget(path, base, tip, [_rv(head, _at(0), 1), _rv(head, _at(20), 9)])
    assert _credit(path, pushed) == {"c9"}


def test_a_credit_across_a_main_merge_survives_moving_past_it(pr, tmp_path):
    """The class audit's unreported variant: a merge of main between the head
    and R, then a commit after R pushed as the tip."""
    path, base, head, fix = pr
    reviews = [_rv(head, _at(0), 1), _rv(head, _at(90), 9)]
    _git(path, "checkout", "-q", "main")
    (path / "g.txt").write_text("main\n")
    _git(path, "add", "g.txt")
    _git(path, "commit", "-q", "-m", "main moves", when=T0 + timedelta(minutes=100))
    _git(path, "update-ref", "refs/remotes/origin/main", "main")
    _git(path, "checkout", "-q", "feat/x")
    _git(path, "merge", "-q", "--no-edit", "main", when=T0 + timedelta(minutes=110))
    _reflect(path, tmp_path, _block(["c9"], fix), T0 + timedelta(minutes=120))
    tip = _change(path, "after the answer\n", T0 + timedelta(minutes=130))
    assert _credit(path, _late_budget(path, base, tip, reviews)) == {"c9"}


def test_a_post_review_fix_after_the_answer_does_not_withdraw_it(tmp_path):
    """The tip moving past R with a real fix of c9 AFTER R is the normal order
    (reflect, then fix): it descends from R and was authored after it."""
    path, head, _, _ = _pushed_before_review(tmp_path)
    _reflect(path, tmp_path, _block(["c9"], head), T0 + timedelta(minutes=30))
    base = _git(path, "rev-list", "--max-parents=0", "HEAD")
    tip = _change(path, "the fix of c9\n", T0 + timedelta(minutes=40))
    budget = _late_budget(path, base, tip, [_rv(head, _at(0), 1), _rv(head, _at(20), 9)])
    assert _credit(path, budget) == {"c9"}


def test_a_fix_on_a_side_branch_merged_in_after_the_answer_refuses(tmp_path):
    """The stated residual, failing closed: a fix of c9 authored after R, but
    on a side branch forked from H and merged in, does not descend from R, so
    R cannot be shown to have come first and it credits nothing."""
    path, head, _, _ = _pushed_before_review(tmp_path)
    _reflect(path, tmp_path, _block(["c9"], head), T0 + timedelta(minutes=30))
    line = _git(path, "rev-parse", "HEAD")
    _git(path, "checkout", "-q", "-b", "side", head)
    (path / "g.txt").write_text("the fix of c9\n")
    _git(path, "add", "g.txt")
    _git(path, "commit", "-q", "-m", "side fix", when=T0 + timedelta(minutes=40))
    _git(path, "checkout", "-q", "feat/x")
    assert _git(path, "rev-parse", "HEAD") == line
    _git(path, "merge", "-q", "--no-ff", "--no-edit", "side", when=T0 + timedelta(minutes=50))
    tip = _git(path, "rev-parse", "HEAD")
    base = _git(path, "rev-list", "--max-parents=0", "HEAD")
    budget = _late_budget(path, base, tip, [_rv(head, _at(0), 1), _rv(head, _at(20), 9)])
    assert _credit(path, budget) == set()


def test_a_missing_head_object_refuses_the_owe_side(pr):
    """Owing must never read "git cannot say" as "no fix": a head object the
    clone lacks refuses (unknown), it does not owe nothing."""
    path, _, _, fix = pr
    with pytest.raises(rr.Refused):
        rl._fixes(str(path), "e" * 40, fix)


def test_a_pre_review_commit_amended_after_the_review_is_a_fix(tmp_path):
    """The later of author and committer time decides on this path too."""
    path, head, _, _ = _pushed_before_review(tmp_path)
    stamp = (T0 + timedelta(minutes=25)).strftime("%Y-%m-%dT%H:%M:%S+0000")
    subprocess.run(
        ["git", "-C", str(path), "commit", "-q", "--amend", "--no-edit"],
        check=True,
        env={**os.environ, "GIT_COMMITTER_DATE": stamp},
    )
    amended = _git(path, "rev-parse", "HEAD")
    base = _git(path, "rev-list", "--max-parents=0", "HEAD")
    budget = _late_budget(path, base, amended, [_rv(head, _at(0), 1), _rv(head, _at(20), 9)])
    _reflect(path, tmp_path, _block(["c9"], head), T0 + timedelta(minutes=30))
    assert _credit(path, budget) == set()


def test_a_side_merge_on_the_late_path_is_judged_by_its_time(tmp_path):
    path, head, pushed, budget = _pushed_before_review(tmp_path)
    _git(path, "checkout", "-q", "-b", "side", "main")
    _git(path, "commit", "-q", "--allow-empty", "-m", "side", when=T0)
    _git(path, "update-ref", "refs/remotes/origin/main", "main")  # side is not in it
    _git(path, "checkout", "-q", "feat/x")
    _git(path, "merge", "-q", "--no-ff", "--no-edit", "side", when=T0 + timedelta(minutes=10))
    _reflect(path, tmp_path, _block(["c9"], head), T0 + timedelta(minutes=30))
    assert _credit(path, budget) == {"c9"}
    _git(path, "reset", "-q", "--hard", pushed)
    _git(path, "merge", "-q", "--no-ff", "--no-edit", "side", when=T0 + timedelta(minutes=25))
    _reflect(path, tmp_path, _block(["c9"], head), T0 + timedelta(minutes=30))
    assert _credit(path, budget) == set()


def test_an_unreadable_commit_time_credits_nothing_on_the_late_path(tmp_path, monkeypatch):
    """The owed list is read first; then every fix walk (the one shared
    reading) returns no times: the credit side credits nothing, and the owe
    side refuses rather than owing nothing."""
    path, head, pushed, budget = _pushed_before_review(tmp_path)
    _reflect(path, tmp_path, _block(["c9"], head), T0 + timedelta(minutes=30))
    late = rl.late_findings(budget, CUT, rl.fix_time_reader(str(path), pushed))
    assert _credit(path, budget) == {"c9"}
    real = rr._run

    def blank_times(argv, cwd=None):
        code, out, err = real(argv, cwd)
        if "--format=%H %T %P|%at %ct" in argv:  # the shared fix walk
            out = "\n".join(row.split("|")[0] + "|" for row in out.split("\n") if row.strip())
        return code, out, err

    monkeypatch.setattr(rr, "_run", blank_times)
    got = rl.covered_late_keys(str(path), budget, late, gate_lane=False, acceptance=None)
    assert got == set()
    with pytest.raises(rr.Refused, match="no readable author time"):
        rl.fix_time_reader(str(path), pushed)(head)


def test_owed_from_budget_with_a_cutover_refuses_past_its_deadline(tmp_path):
    path, head, _, budget = _pushed_before_review(tmp_path)
    _reflect(path, tmp_path, _block(["c9"], head), T0 + timedelta(minutes=30))
    with pytest.raises(rr.Refused, match="ran out of time"):
        rr.owed_from_budget(str(path), budget, "o/r", 7, now=T0, cutover=CUT, deadline=0.0)


def test_a_main_merge_after_the_review_is_no_fix_on_the_way_to_a_later_head(tmp_path):
    """Secondary review P2: a fix F, then review c9 of H, then a clean merge of
    main, then a reflection naming the merged head N. The latest fix on the way
    from H to N is F (before c9), not the merge, so c9 is answered at N."""
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
    _change(path, "fixed\n", T0 + timedelta(minutes=5))
    _git(path, "checkout", "-q", "main")
    (path / "g.txt").write_text("main\n")
    _git(path, "add", "g.txt")
    _git(path, "commit", "-q", "-m", "main moves", when=T0 + timedelta(minutes=22))
    _git(path, "update-ref", "refs/remotes/origin/main", "main")
    _git(path, "checkout", "-q", "feat/x")
    _git(path, "merge", "-q", "--no-edit", "main", when=T0 + timedelta(minutes=25))
    merged = _git(path, "rev-parse", "HEAD")
    budget = _late_budget(path, base, merged, [_rv(head, _at(0), 1), _rv(head, _at(20), 9)])
    reader = rl.fix_time_reader(str(path), merged)
    assert [i["key"] for i in rl.late_findings(budget, CUT, reader)] == ["c9"]
    _reflect(path, tmp_path, _block(["c9"], merged), T0 + timedelta(minutes=30))
    assert _credit(path, budget) == {"c9"}
    # The owe side still counts the merge: owing stays early, never late.
    assert rl._fix_times(str(path), head, merged)[-1] == T0 + timedelta(minutes=25)


def test_covered_late_keys_bounds_its_own_git_calls(pr, tmp_path, monkeypatch):
    """Secondary review P3: the deadline argument scopes every git call, not
    only the top of each reflection's turn."""
    path, base, head, fix = pr
    budget = _late_budget(path, base, fix, [_rv(head, _at(0), 1), _rv(head, _at(90), 9)])
    _reflect(path, tmp_path, _block(["c9"], fix), T0 + timedelta(minutes=120))
    late = rl.late_findings(budget, CUT, rl.fix_time_reader(str(path), fix))
    seen = []
    real = rr.fix_rows

    def spy(*args, **kwargs):
        seen.append(rr._DEADLINE.get())
        return real(*args, **kwargs)

    monkeypatch.setattr(rr, "fix_rows", spy)
    far = 10**12
    assert rl.covered_late_keys(
        str(path), budget, late, gate_lane=False, acceptance=None, deadline=far
    ) == {"c9"}
    assert seen and all(bound == far for bound in seen)


def test_a_main_merge_between_the_head_and_the_answer_is_no_fix(pr, tmp_path):
    path, base, head, fix = pr
    reviews = [_rv(head, _at(0), 1), _rv(head, _at(90), 9)]
    budget = _late_budget(path, base, fix, reviews)
    _git(path, "checkout", "-q", "main")
    (path / "g.txt").write_text("main\n")
    _git(path, "add", "g.txt")
    _git(path, "commit", "-q", "-m", "main moves", when=T0 + timedelta(minutes=100))
    _git(path, "update-ref", "refs/remotes/origin/main", "main")
    _git(path, "checkout", "-q", "feat/x")
    _git(path, "merge", "-q", "--no-edit", "main", when=T0 + timedelta(minutes=110))
    _reflect(path, tmp_path, _block(["c9"], fix), T0 + timedelta(minutes=120))
    assert _credit(path, budget) == {"c9"}


def test_two_reflections_credit_the_union(pr, tmp_path):
    path, base, head, fix = pr
    reviews = [_rv(head, _at(0), 1), _rv(head, _at(85), 8), _rv(head, _at(90), 9)]
    budget = _late_budget(path, base, fix, reviews)
    _reflect(path, tmp_path, _block(["c8"], fix), T0 + timedelta(minutes=120))
    _reflect(path, tmp_path, _block(["c9"], fix), T0 + timedelta(minutes=121))
    assert _credit(path, budget) == {"c8", "c9"}


def test_a_reflection_with_no_readable_time_credits_nothing(pr, tmp_path, monkeypatch):
    path, base, head, fix = pr
    budget = _late_budget(path, base, fix, [_rv(head, _at(0), 1), _rv(head, _at(90), 9)])
    sha = _reflect(path, tmp_path, _block(["c9"], fix), T0 + timedelta(minutes=120))
    monkeypatch.setattr(
        rr, "_log_reflections", lambda cwd: [(sha, "empty", _block(["c9"], fix), None)]
    )
    assert _credit(path, budget) == set()


def test_a_review_of_a_head_outside_the_graph_is_never_credited(pr, tmp_path):
    """Even handed such a key directly, crediting never places it."""
    path, base, head, fix = pr
    budget = _late_budget(path, base, fix, [_rv(head, _at(0), 1)])
    _reflect(path, tmp_path, _block(["c9"], fix), T0 + timedelta(minutes=120))
    late = [{"head": GONE, "key": "c9", "at": T0 + timedelta(minutes=90), "round_head": head}]
    assert rl.covered_late_keys(str(path), budget, late, gate_lane=False, acceptance=None) == set()


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
    assert rl._fix_times(str(path), head, merged) == [T0 + timedelta(minutes=40)]
    assert rl.fix_time_reader(str(path), merged)(head) == T0 + timedelta(minutes=40)


# -- the PR line, read without merge parent order (Codex c4278817060, c4278817070) --


def test_a_side_fix_forked_from_the_head_counts_from_its_own_commit(pr, tmp_path):
    """c4278817060, accepted as a fail-closed residual: a fix on a branch
    forked from H counts from its own commit, before the merge brings it to
    the PR, because no reading of merge parent order is safe (a pull reverses
    it). A review between the two is owed early, and stays answerable."""
    path, base, head, _ = pr
    _git(path, "checkout", "-q", "-b", "side", head)
    (path / "g.txt").write_text("side fix\n")
    _git(path, "add", "g.txt")
    _git(path, "commit", "-q", "-m", "side fix", when=T0 + timedelta(minutes=30))
    _git(path, "checkout", "-q", "-b", "line", head)
    _git(
        path, "merge", "-q", "--no-ff", "-m", "merge side", "side", when=T0 + timedelta(minutes=100)
    )
    merged = _git(path, "rev-parse", "HEAD")
    assert rl._fix_times(str(path), head, merged) == [
        T0 + timedelta(minutes=30),
        T0 + timedelta(minutes=100),
    ]
    budget = _late_budget(path, base, merged, [_rv(head, _at(0), 1), _rv(head, _at(50), 9)])
    late = rl.late_findings(budget, CUT, rl.fix_time_reader(str(path), merged))
    assert [i["key"] for i in late] == ["c9"]


def test_a_pull_merge_from_a_stale_branch_never_hides_a_pushed_fix(pr, tmp_path):
    """The fix-code audit's case: F is pushed, c9 lands on H after it, then a
    stale branch at H commits and pulls F in, putting F on the merge's second
    parent. The earliest fix is still F, so c9 stays owed."""
    path, base, head, fix = pr
    _git(path, "checkout", "-q", "-b", "stale", head)
    (path / "g.txt").write_text("stale work\n")
    _git(path, "add", "g.txt")
    _git(path, "commit", "-q", "-m", "stale work", when=T0 + timedelta(minutes=120))
    _git(path, "merge", "-q", "--no-ff", "-m", "pull", "feat/x", when=T0 + timedelta(minutes=130))
    merged = _git(path, "rev-parse", "HEAD")
    assert rl.fix_time_reader(str(path), merged)(head) == T0 + timedelta(minutes=60)
    budget = _late_budget(path, base, merged, [_rv(head, _at(0), 1), _rv(head, _at(90), 9)])
    late = rl.late_findings(budget, CUT, rl.fix_time_reader(str(path), merged))
    assert [i["key"] for i in late] == ["c9"]


def _fork_reflection(path, tmp_path, head):
    """A reflection naming H, made on a fork of H after the fix F existed."""
    _git(path, "checkout", "-q", "-b", "fork", head)
    sha = _reflect(path, tmp_path, _block(["c9"], head), T0 + timedelta(minutes=120))
    return sha


def test_a_reflection_on_a_fork_never_credits(pr, tmp_path):
    """c4278817070: the head a reflection names is on the line, but the
    reflection commit is not; a rationalisation made after the fix, on a
    fork, credits nothing."""
    path, base, head, fix = pr
    budget = _late_budget(path, base, fix, [_rv(head, _at(0), 1), _rv(head, _at(90), 9)])
    _fork_reflection(path, tmp_path, head)
    assert _credit(path, budget) == set()


def _late_review_then_fix(path, tmp_path, head):
    """H, an empty commit E (reviewed clean at +10, which makes the review of H
    at +20 late), then the fix F at +60: F is a fix of c9."""
    _git(path, "reset", "-q", "--hard", head)
    empty = _reflect(path, tmp_path, "wip\n", T0 + timedelta(minutes=5))
    _change(path, "z\n", T0 + timedelta(minutes=60))
    return [_rv(empty, _at(10)), _rv(head, _at(20), 9)]


def test_a_fork_reflection_merged_in_still_never_credits(pr, tmp_path):
    """Merged in as a second parent, the fork's reflection becomes an ancestor
    of the tip, which an ancestor-either-way check would accept; the fix F of
    c9 still does not descend from it, so it credits nothing."""
    path, base, head, _ = pr
    reviews = _late_review_then_fix(path, tmp_path, head)
    _fork_reflection(path, tmp_path, head)
    _git(path, "checkout", "-q", "feat/x")
    _git(
        path, "merge", "-q", "--no-ff", "-m", "merge fork", "fork", when=T0 + timedelta(minutes=130)
    )
    merged = _git(path, "rev-parse", "HEAD")
    budget = _late_budget(path, base, merged, reviews)
    late = rl.late_findings(budget, CUT, rl.fix_time_reader(str(path), merged))
    assert [i["key"] for i in late] == ["c9"]
    assert rl.covered_late_keys(str(path), budget, late, gate_lane=False, acceptance=None) == set()


def test_a_fork_reflection_that_pulls_the_fix_in_still_never_credits(pr, tmp_path):
    """The other orientation: the fork merges the PR branch into itself, so
    the reflection is on the merge's FIRST parent; F still does not descend
    from it, so it credits nothing."""
    path, base, head, _ = pr
    reviews = _late_review_then_fix(path, tmp_path, head)
    _fork_reflection(path, tmp_path, head)
    _git(path, "merge", "-q", "--no-ff", "-m", "pull", "feat/x", when=T0 + timedelta(minutes=130))
    merged = _git(path, "rev-parse", "HEAD")
    budget = _late_budget(path, base, merged, reviews)
    late = rl.late_findings(budget, CUT, rl.fix_time_reader(str(path), merged))
    assert [i["key"] for i in late] == ["c9"]
    assert rl.covered_late_keys(str(path), budget, late, gate_lane=False, acceptance=None) == set()


def test_a_fork_reflection_merged_in_after_a_pre_review_commit_credits(pr, tmp_path):
    """The per-key rule (owner ruling 2026-10-20): F was committed before
    review c9 landed, so F is no fix of c9, and a reflection made after F, on
    a fork of H merged into the line, answers c9."""
    path, base, head, fix = pr
    _fork_reflection(path, tmp_path, head)
    _git(path, "checkout", "-q", "feat/x")
    _git(
        path, "merge", "-q", "--no-ff", "-m", "merge fork", "fork", when=T0 + timedelta(minutes=130)
    )
    merged = _git(path, "rev-parse", "HEAD")
    budget = _late_budget(path, base, merged, [_rv(head, _at(0), 1), _rv(head, _at(90), 9)])
    late = rl.late_findings(budget, CUT, rl.fix_time_reader(str(path), merged))
    assert [i["key"] for i in late] == ["c9"]
    assert rl.covered_late_keys(str(path), budget, late, gate_lane=False, acceptance=None) == {"c9"}


def test_a_reflection_the_tip_never_reached_never_credits(pr, tmp_path):
    """With no fix after H, every fix trivially descends from any reflection;
    a reflection on a fork the PR tip does not contain (and is not ahead of)
    still credits nothing: it is not on the PR at all."""
    path, base, head, _ = pr
    _git(path, "reset", "-q", "--hard", head)
    empty = _reflect(path, tmp_path, "wip\n", T0 + timedelta(minutes=2))
    budget = _late_budget(path, base, empty, [_rv(empty, _at(3)), _rv(head, _at(5), 9)])
    assert budget["findings"][0]["late"] is True
    _git(path, "checkout", "-q", "-b", "fork", head)
    _reflect(path, tmp_path, _block(["c9"], head), T0 + timedelta(minutes=8))
    assert _credit(path, budget) == set()


def test_a_fix_reordered_after_the_reflection_still_counts_before_it(pr, tmp_path):
    """The re-audit's case: the fix F (authored +60) is reordered after a
    reflection R (+120) by a rebase and force-pushed. F now descends from R,
    but it was authored first, and the owe rule dates it there."""
    path, base, head, _ = pr
    budget_reviews = [_rv(head, _at(0), 1), _rv(head, _at(90), 9)]
    _git(path, "checkout", "-q", "-b", "reordered", head)
    _reflect(path, tmp_path, _block(["c9"], head), T0 + timedelta(minutes=120))
    # The fix keeps its author time (+60); only the committer time moves.
    stamp = (T0 + timedelta(minutes=125)).strftime("%Y-%m-%dT%H:%M:%S+0000")
    subprocess.run(
        ["git", "-C", str(path), "cherry-pick", "feat/x"],
        check=True,
        capture_output=True,
        env={**os.environ, "GIT_COMMITTER_DATE": stamp},
    )
    tip = _git(path, "rev-parse", "HEAD")
    assert rl.fix_time_reader(str(path), tip)(head) == T0 + timedelta(minutes=60)
    budget = _late_budget(path, base, tip, budget_reviews)
    assert _credit(path, budget) == set()


def test_a_fork_reflection_before_a_sibling_fix_is_refused(pr, tmp_path):
    """Stated residual, failing closed: R is made on a fork of H before any
    fix exists, a fix F then lands on the PR line, and the fork is merged.
    F was authored after R but does not descend from it, so R is refused;
    by time alone it would credit."""
    path, base, head, _ = pr
    _git(path, "reset", "-q", "--hard", head)
    empty = _reflect(path, tmp_path, "wip\n", T0 + timedelta(minutes=2))
    _git(path, "checkout", "-q", "-b", "fork", head)
    _reflect(path, tmp_path, _block(["c9"], head), T0 + timedelta(minutes=8))
    _git(path, "checkout", "-q", "feat/x")
    _change(path, "f\n", T0 + timedelta(minutes=20))
    _git(
        path, "merge", "-q", "--no-ff", "-m", "merge fork", "fork", when=T0 + timedelta(minutes=30)
    )
    tip = _git(path, "rev-parse", "HEAD")
    budget = _late_budget(path, base, tip, [_rv(empty, _at(3)), _rv(head, _at(5), 9)])
    assert budget["findings"][0]["late"] is True
    assert _credit(path, budget) == set()


def test_each_reflection_is_judged_by_its_own_heads_fixes(pr, tmp_path):
    """Pins the memo key: R2 (naming the merge M) has only the later fix K
    after its head, while R1 (a fork reflection naming H) has every fix. If
    R1 were judged by R2's set it would credit c9."""
    path, base, head, fix = pr
    second = _change(path, "z2\n", T0 + timedelta(minutes=100))
    _fork_reflection(path, tmp_path, head)
    _git(path, "checkout", "-q", "feat/x")
    _git(
        path, "merge", "-q", "--no-ff", "-m", "merge fork", "fork", when=T0 + timedelta(minutes=110)
    )
    merged = _git(path, "rev-parse", "HEAD")
    _reflect(path, tmp_path, _block(["c7"], merged), T0 + timedelta(minutes=115))
    tip = _change(path, "z3\n", T0 + timedelta(minutes=120))
    assert second != tip
    budget = _late_budget(path, base, tip, [_rv(head, _at(0), 1), _rv(head, _at(80), 9)])
    assert _credit(path, budget) == set()


def test_a_pushed_reflection_on_the_line_credits(pr, tmp_path):
    """The control: a reflection on the PR line before the fix, already
    pushed (the tip descends from it), credits its late key."""
    path, base, head, _ = pr
    _git(path, "reset", "-q", "--hard", head)
    empty = _reflect(path, tmp_path, "wip\n", T0 + timedelta(minutes=2))
    _reflect(path, tmp_path, _block(["c9"], head), T0 + timedelta(minutes=8))
    fix = _change(path, "z\n", T0 + timedelta(minutes=60))
    reviews = [_rv(empty, _at(3)), _rv(head, _at(5), 9)]
    budget = _late_budget(path, base, fix, reviews)
    assert budget["findings"][0]["late"] is True
    assert _credit(path, budget) == {"c9"}


# -- earlier rounds' classes, by ancestry (Codex c4273454993) ----------------------


def _two_round_answer(path, tmp_path, base, head, fix):
    """Round 1 at H (+0), round 2 at F (+70), then late review c9 on F (+110)
    after G (+100) was reviewed clean. The answer at G is judged under round
    2: ordinary lane owes a premise check, and its class repeats nothing."""
    second = _change(path, "z2\n", T0 + timedelta(minutes=100))
    reviews = [
        _rv(head, _at(0), 1),
        _rv(fix, _at(70), 2),
        _rv(second, _at(105)),
        _rv(fix, _at(110), 9),
    ]
    budget = _late_budget(path, base, second, reviews)
    item = [f for f in budget["findings"] if f["keys"] == ["c9"]][0]
    assert item["late"] is True and item["round_head"] == fix
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
    return budget, second, answer


def test_a_reflection_made_later_never_withdraws_an_earlier_credit(pr, tmp_path):
    """A round-1 reflection committed AFTER the answer cannot turn its class
    into a recurring one: it is not the answer's ancestor."""
    path, base, head, fix = pr
    budget, second, answer = _two_round_answer(path, tmp_path, base, head, fix)
    _reflect(path, tmp_path, answer, T0 + timedelta(minutes=120))
    assert _credit(path, budget) == {"c9"}
    _reflect(path, tmp_path, _block(["c1"], head), T0 + timedelta(minutes=130))
    assert _credit(path, budget) == {"c9"}


def test_a_reflection_merged_in_after_the_answer_never_counts(pr, tmp_path):
    """Codex c4273454993: a round-1 reflection made BEFORE the answer, on a
    side branch merged in after it, is in HEAD's history and older by
    timestamp, but it is not the answer's git ancestor, so its class never
    counts against the answer."""
    path, base, head, fix = pr
    budget, second, answer = _two_round_answer(path, tmp_path, base, head, fix)
    _git(path, "checkout", "-q", "-b", "side", head)
    _reflect(path, tmp_path, _block(["c1"], head), T0 + timedelta(minutes=116))
    _git(path, "checkout", "-q", "feat/x")
    _reflect(path, tmp_path, answer, T0 + timedelta(minutes=120))
    _git(
        path, "merge", "-q", "--no-ff", "-m", "merge side", "side", when=T0 + timedelta(minutes=125)
    )
    assert _credit(path, budget) == {"c9"}
    # The control: the same round-1 reflection as the answer's ancestor
    # does make the class recurring, and refuses the fix-instances answer.
    _git(path, "checkout", "-q", "-b", "ancestor", second)
    _reflect(path, tmp_path, _block(["c1"], head), T0 + timedelta(minutes=116))
    _reflect(path, tmp_path, answer, T0 + timedelta(minutes=120))
    assert _credit(path, budget) == set()


def test_an_unreadable_ancestry_probe_refuses(monkeypatch):
    """S3: git's exit 0 is an ancestor, 1 is not; anything else is unknown."""
    monkeypatch.setattr(rr, "_run", lambda argv, cwd=None: (128, "", "fatal"))
    with pytest.raises(rr.Refused):
        rr._is_ancestor("/nowhere", H1, H2)
    monkeypatch.setattr(rr, "_run", lambda argv, cwd=None: (1, "", ""))
    assert rr._is_ancestor("/nowhere", H1, H2) is False


# -- status, end to end -----------------------------------------------------------


def _status_env(monkeypatch, budget):
    budget["body"] = ""
    monkeypatch.setattr(rr, "pr_identity", lambda cwd: ("o/r", 1))
    monkeypatch.setattr(rb, "evaluate_pr", lambda repo, number: budget)


def test_status_owes_late_keys_only_from_a_cutover(pr, tmp_path, monkeypatch, capsys):
    path, base, head, fix = pr
    budget = _late_budget(path, base, fix, [_rv(head, _at(0), 1), _rv(head, _at(90), 9)])
    _status_env(monkeypatch, budget)
    monkeypatch.setattr(rr, "REFLECTION_CUTOVER_ISO", None)
    uncut = rr.status(str(path), now=T0)
    assert uncut["owed"] == [] and uncut["cutover"] is None
    monkeypatch.setattr(rr, "REFLECTION_CUTOVER_ISO", CUT)
    assert rr.status(str(path), now=T0)["owed"] == ["c9"]
    _reflect(path, tmp_path, _block(["c9"], fix), T0 + timedelta(minutes=120))
    assert rr.status(str(path), now=T0)["owed"] == []
    monkeypatch.setattr(rr, "REFLECTION_CUTOVER_ISO", None)
    monkeypatch.chdir(path)
    assert rr.main(["status", "--cutover", CUT]) == 0
    assert '"late_keys": [\n    "c9"\n  ]' in capsys.readouterr().out


def test_status_reads_no_pr_body_for_a_round_zero_only_debt(pr, tmp_path, monkeypatch):
    """Round 0 owes no acceptance check, so a body that changed between the
    budget's two reads never makes a round-0-only debt unknown; a debt under
    a real round still refuses."""
    path, base, head, fix = pr
    budget = _round_zero(path, base, head, fix)
    _status_env(monkeypatch, budget)
    budget["body_changed"] = True
    assert rr.status(str(path), now=T0, cutover=CUT)["owed"] == ["c9"]
    other = _late_budget(path, base, fix, [_rv(head, _at(0), 1), _rv(head, _at(90), 9)])
    _status_env(monkeypatch, other)
    other["body_changed"] = True
    with pytest.raises(rr.Refused):
        rr.status(str(path), now=T0, cutover=CUT)


def test_an_unusable_author_time_refuses(pr, tmp_path):
    path, base, head, fix = pr
    (path / "f.txt").write_text("far\n")
    subprocess.run(
        ["git", "-C", str(path), "commit", "-q", "-am", "far"],
        check=True,
        env={**os.environ, "GIT_AUTHOR_DATE": "@99999999999999 +0000"},
    )
    far = _git(path, "rev-parse", "HEAD")
    with pytest.raises(rr.Refused):
        rl._fix_times(str(path), head, far)


def test_status_never_lets_the_open_rounds_coverage_clear_a_late_key(pr, monkeypatch):
    path, base, head, fix = pr
    reviews = [_rv(head, _at(0), 1), _rv(head, _at(90), 9), _rv(fix, _at(95), 2)]
    budget = _late_budget(path, base, fix, reviews)
    assert budget["round_state"] == "open" and budget["open_keys"] == ["c2"]
    _status_env(monkeypatch, budget)
    monkeypatch.setattr(rr, "covered_keys", lambda *a, **k: {"c2", "c9"})
    monkeypatch.setattr(rl, "covered_late_keys", lambda *a, **k: set())
    assert rr.status(str(path), now=T0, cutover=CUT)["owed"] == ["c9"]
