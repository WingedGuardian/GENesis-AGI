"""The round reflection: the closed header-block grammar and its coverage check.

The format is a closed block of fixed ``Field: value`` lines (owner ruling
2026-10-07), so most tests are tables: every markdown construct the earlier
regex parser had to special-case is now simply refused inside the block and
ignored in the prose after it. Coverage tests run in real temporary git
repositories, because emptiness, ancestry, hooks and encodings are git
behaviours.
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

HEAD = "a" * 40
T0 = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)


def _reflection(
    keys=("c1", "c2"),
    *,
    head=HEAD,
    premise="LEAD",
    decision="close-class",
    classes=("unchecked subprocess result = 2",),
    dispositions=None,
    scopes=("covered: the key round-trips (tests/test_x.py)",),
    escalate="no",
    extra=(),
    prose="",
):
    if dispositions is None:
        dispositions = [f"Disposition: {k} fix-now test=1" for k in keys]
    block = [
        f"Round-reflection: keys={','.join(keys)} head={head}",
        *[f"Class: {c}" for c in classes],
        f"Premise: {premise}",
        "Premise-why: the change routes around an existing helper that already exists",
        "Impossible: route every call through one checked helper and delete the rest",
        *[f"Scope: {s}" for s in scopes],
        f"Decision: {decision}",
        "Decision-why: the same mistake appears twice in two different files",
        *dispositions,
        *extra,
        f"Escalate: {escalate}",
    ]
    text = "\n".join(block) + "\n"
    if prose:
        text += "\n" + prose + "\n"
    return text


# -- grammar -----------------------------------------------------------------


@pytest.mark.parametrize(
    "round_number, gate, want",
    [
        (1, False, (False, False)),
        (2, False, (False, True)),  # premise check first (owner ruling 2026-10-08)
        (3, False, (True, True)),  # then the class sweep, premise still owed
        (4, False, (False, True)),
        (1, True, (True, False)),
        (2, True, (False, True)),
    ],
)
def test_obligations_follow_the_lane_ladder(round_number, gate, want):
    assert rr.obligations(round_number, gate) == want


def test_a_complete_round_one_reflection_validates():
    got = rr.parse(_reflection(), round_number=1)
    assert got.ok, got.problems
    assert got.keys == ["c1", "c2"] and got.head == HEAD
    assert got.classes == ["unchecked subprocess result"]
    assert got.decision == "close-class" and got.verdict == "LEAD" and not got.escalate


def test_all_key_forms_are_in_the_grammar():
    assert rr.parse(_reflection(keys=("c1", "r20:2", "i30")), round_number=1).ok
    assert not rr.parse(_reflection(keys=("r20/1",))).ok


@pytest.mark.parametrize(
    "construct",
    [
        "## Distribution",
        "- Class: dashed = 1",
        "1. Class: numbered = 1",
        "+ Class: plus = 1",
        "```",
        "~~~",
        "<!-- Disposition: c9 fix-now test=1 -->",
        "**Premise:** LEAD",
        "Class: `backticked` = 1",
        "Class: **bold** = 1",
        "  Escalate: no",
        "Escalate： no",  # fullwidth colon
        "Scope: a non-breaking space before this value",
        "Scope: zero​width space hiding inside this value",
        "Scope: a carriage return at the end of this value\r",
        "Disposition: c1 fix-now test=1 AND file issue=#2",
        "Disposition: c1 fix-now (test 1)",
        "Premise-check: P1 is it TRUE? nobody checked",
        "Random: an unknown field name is refused",
        "Decision: maybe",
        # Round 2 of #3055: value domains start at 1.
        "Class: an empty class = 0",
        "Disposition: c1 file issue=#0",
        "Premise-check: P01 TRUE a leading zero makes a second P1",
    ],
)
def test_every_markdown_or_malformed_construct_in_the_block_is_refused(construct):
    """Round 1 of #3055: each of these was its own hole in the markdown
    reader. In the closed block each is simply an unrecognised line."""
    got = rr.parse(_reflection(extra=[construct]))
    assert not got.ok, construct
    assert any(p.startswith("line ") for p in got.problems), got.problems


@pytest.mark.parametrize(
    "line",
    [
        "Disposition: c١ fix-now test=1",  # an Arabic-Indic digit
        "Scope: a non breaking space inside this value",
    ],
)
def test_a_non_ascii_character_in_the_block_is_named_as_such(line):
    got = rr.parse(_reflection(extra=[line]))
    assert any("only printable ASCII" in p for p in got.problems), got.problems


def test_a_unicode_digit_in_a_header_key_is_refused():
    text = _reflection().replace("keys=c1", "keys=c١", 1)
    assert text != _reflection()
    assert rr.HEADER_RE.match(text.split("\n")[0]) is None


@pytest.mark.parametrize(
    "prose",
    [
        "## Notes\nDisposition: c9 fix-now test=1",
        "```\nanything at all\n```",
        "<!-- a comment -->",
        "- a list\n1. another",
    ],
)
def test_prose_after_the_block_is_never_parsed(prose):
    got = rr.parse(_reflection(prose=prose), round_number=1)
    assert got.ok, got.problems
    assert got.keys == ["c1", "c2"]


@pytest.mark.parametrize(
    "text, fragment",
    [
        ("\n" + _reflection(), "line 1 must be"),
        (" " + _reflection(), "line 1 must be"),
        (_reflection(keys=("c1", "c1")), "names a key twice"),
        (_reflection(dispositions=["Disposition: c1 fix-now test=1"]), "no disposition for: c2"),
        (
            _reflection(
                dispositions=[
                    "Disposition: c1 fix-now test=1",
                    "Disposition: c2 file issue=#9",
                    "Disposition: c3 fix-now test=2",
                ]
            ),
            "header does not name: c3",
        ),
        (
            _reflection(
                dispositions=[
                    "Disposition: c1 fix-now test=1",
                    "Disposition: c1 file issue=#2",
                    "Disposition: c2 fix-now test=1",
                ]
            ),
            "two dispositions",
        ),
        (_reflection(extra=["Premise: SOUND"]), "'Premise:' appears 2 times"),
        (_reflection(extra=["Decision: rework"]), "'Decision:' appears 2 times"),
        (_reflection(classes=()), "missing 'Class:' line"),
        (_reflection(scopes=()), "missing 'Scope:' line"),
        (_reflection(extra=[f"Scope: {rr.FILL} later>"]), "unfilled template placeholder"),
    ],
)
def test_structure_problems_are_named(text, fragment):
    got = rr.parse(text)
    assert not got.ok and any(fragment in p for p in got.problems), got.problems


def test_a_short_block_is_refused():
    text = f"Round-reflection: keys=c1 head={HEAD}\nDisposition: c1 fix-now test=1\n"
    assert any("at least" in p for p in rr.parse(text).problems)


@pytest.mark.parametrize(
    "text",
    [
        _reflection(escalate="yes"),
        _reflection(premise="BROKEN"),
        _reflection(premise="SUSPECT"),
        _reflection(prose="We think the Premise: **BROKEN** after all."),
        _reflection(prose="Ｅｓｃａｌａｔｅ： yes"),
        _reflection(prose="Esc​alate: yes"),
    ],
)
def test_escalation_fails_toward_escalating(text):
    """Round 1 of #3055: an escalating label anywhere, in any form NFKC folds
    to, escalates, so placement or a lookalike can never hide it."""
    assert rr.parse(text).escalate


def test_lead_is_only_a_round_one_verdict():
    assert any("from round 2" in p for p in rr.parse(_reflection(), round_number=2).problems)


AUDIT_LINE = "Audit-evidence: ~/.genesis/review_evidence/x.txt NO BLOCKER"
CHECK_LINES = [
    "Premise-check: P1 TRUE the store already dedups by key",
    "Premise-check: P2 FALSE replies are never top level",
]
PREMISE_LINES = [*CHECK_LINES, "Premise-evidence: ~/.genesis/review_evidence/p.txt SOUND"]


def _problems(*extra, round_number, gate_lane=False):
    sound = "SOUND-BUT-INFERIOR"
    return rr.parse(
        _reflection(premise=sound, extra=list(extra)),
        round_number=round_number,
        gate_lane=gate_lane,
    ).problems


def test_round_obligations_by_lane():
    # gate lane: class audit at round 1, premise check at round 2
    assert any(
        "fresh-context audit" in p
        for p in rr.parse(_reflection(), round_number=1, gate_lane=True).problems
    )
    assert rr.parse(_reflection(extra=[AUDIT_LINE]), round_number=1, gate_lane=True).ok
    assert any("premise check" in p for p in _problems(round_number=2, gate_lane=True))
    assert not _problems(*PREMISE_LINES, round_number=2, gate_lane=True)
    twice = [CHECK_LINES[0], CHECK_LINES[0], PREMISE_LINES[-1]]
    again = _problems(*twice, round_number=2, gate_lane=True)
    assert any("premise check" in p for p in again), "one check written twice"
    # ordinary lane (owner ruling 2026-10-08): premise check from round 2,
    # the class sweep at round 3
    assert any("premise check" in p for p in _problems(AUDIT_LINE, round_number=2))
    assert not _problems(*PREMISE_LINES, round_number=2)
    assert any("fresh-context audit" in p for p in _problems(*PREMISE_LINES, round_number=3))
    assert not _problems(AUDIT_LINE, *PREMISE_LINES, round_number=3)
    assert not _problems(*PREMISE_LINES, round_number=4)


def test_premise_lines_without_their_evidence_file_are_refused():
    got = _problems(*CHECK_LINES, round_number=2)
    assert any("cites the premise check's output" in p for p in got), got
    assert not rr.parse(_reflection(extra=CHECK_LINES), round_number=1).problems


def test_a_recurring_class_forbids_fixing_instances():
    text = _reflection(decision="fix-instances")
    got = rr.parse(text, round_number=1, previous_classes=["Unchecked  subprocess result"])
    assert any("recurs" in p for p in got.problems)
    assert rr.parse(
        _reflection(), round_number=1, previous_classes=["unchecked subprocess result"]
    ).ok


def test_each_acceptance_point_must_appear_in_a_scope_line():
    point = "Every finding is keyed by an immutable id"
    missing = rr.parse(_reflection(), round_number=1, acceptance=[point])
    assert any("acceptance point" in p for p in missing.problems)
    mapped = _reflection(scopes=(f"covered: {point.lower()} (tests/test_x.py)",))
    assert rr.parse(mapped, round_number=1, acceptance=[point]).ok


# -- what is owed ------------------------------------------------------------


def _budget(**over):
    budget = {
        "status": "ok",
        "count": 1,
        "round_state": "open",
        "current_head": HEAD,
        "gate_surface": False,
        "reflection_keys": "ok",
        "open_keys": ["c1", "c2", "r5:1"],
        "rounds": [
            {
                "head": HEAD,
                "reviews": [
                    {"submitted_at": T0.isoformat()},
                    {"submitted_at": (T0 + timedelta(minutes=5)).isoformat()},
                ],
            }
        ],
        "expected_reviewers": [],
        "reviewers_reported": ["chatgpt-codex-connector[bot]"],
    }
    if "current_head" in over and "rounds" not in over:
        # The open round is the head's own entry (#3302): keep the default
        # round on whatever head the test names, as the producer would.
        budget["rounds"] = [dict(budget["rounds"][0], head=over["current_head"])]
    budget.update(over)
    return budget


def test_nothing_is_owed_unless_a_round_is_open():
    assert rr.owed_state(_budget(round_state="complete"), [], now=T0)["owed"] == []


def test_unknown_evidence_owes_unknown_never_nothing():
    assert rr.owed_state(_budget(status="unknown"), [], now=T0)["owed"] is None
    partial = _budget(reflection_keys="unknown")
    assert rr.owed_state(partial, ["c1", "c2", "r5:1"], now=T0)["owed"] is None


def test_covered_keys_are_not_owed():
    assert rr.owed_state(_budget(), ["c2"], now=T0)["owed"] == ["c1", "r5:1"]


def test_the_round_settles_thirty_minutes_after_its_first_review():
    assert not rr.owed_state(_budget(), [], now=T0 + timedelta(minutes=29))["settled"]
    got = rr.owed_state(_budget(), [], now=T0 + timedelta(minutes=30))
    assert got["settled"] and got["settle_until"] == (T0 + timedelta(minutes=30)).isoformat()
    assert got["round_started"] == T0.isoformat() and got["round_head"] == HEAD


def test_it_settles_early_once_every_expected_reviewer_has_reported():
    early = T0 + timedelta(minutes=1)
    waiting = _budget(expected_reviewers=["a[bot]", "b[bot]"], reviewers_reported=["a[bot]"])
    assert not rr.owed_state(waiting, [], now=early)["settled"]
    both = dict(waiting, reviewers_reported=["a[bot]", "b[bot]"])
    assert rr.owed_state(both, [], now=early)["settled"]


def test_round_one_with_nobody_expected_waits_the_whole_window():
    got = rr.owed_state(_budget(expected_reviewers=[]), [], now=T0 + timedelta(minutes=1))
    assert not got["settled"]


def test_no_readable_review_time_never_settles():
    got = rr.owed_state(_budget(rounds=[{"reviews": [{"submitted_at": None}]}]), [], now=T0)
    assert not got["settled"] and got["settle_until"] is None


@pytest.mark.parametrize(
    "parsed, want",
    [
        ({"present": True, "bullets": ["a"], "problems": []}, ["a"]),
        ({"present": False, "bullets": [], "problems": ["no ## Acceptance section"]}, None),
        ({"present": False, "bullets": [], "problems": ["empty PR body"]}, None),
    ],
)
def test_acceptance_points_present_or_absent(monkeypatch, parsed, want):
    import acceptance_declaration

    monkeypatch.setattr(acceptance_declaration, "parse_acceptance", lambda body: parsed)
    assert rr.acceptance_points("body") == want


@pytest.mark.parametrize(
    "problem",
    [
        "cannot load readable_body from check_cc_pin_receipts.py",
        "cannot read this body reliably: a code fence is indented",
        "## Acceptance has no bullets",
        "body too large to verify (99999 chars)",
    ],
)
def test_an_unreadable_acceptance_declaration_refuses(monkeypatch, problem):
    """Round 1 of #3055: unreadable is not absent; it must never silently
    disable the scope rule."""
    import acceptance_declaration

    monkeypatch.setattr(
        acceptance_declaration,
        "parse_acceptance",
        lambda body: {"present": False, "bullets": [], "problems": [problem]},
    )
    with pytest.raises(rr.Refused, match="cannot be read reliably"):
        rr.acceptance_points("body")


# -- coverage, in a real repository ------------------------------------------


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout


def _commit_reflection(repo: Path, tmp_path: Path, text: str, *, empty: bool = True) -> str:
    message = tmp_path / "message.md"
    message.write_text(text)
    if not empty:
        (repo / "f.txt").write_text((repo / "f.txt").read_text() + "x\n")
        _git(repo, "add", "f.txt")
    flags = ["--allow-empty"] if empty else []
    _git(repo, "commit", "-q", *flags, "--cleanup=verbatim", "-F", str(message))
    return _git(repo, "rev-parse", "HEAD").strip()


def _fix(repo: Path, content: str) -> None:
    (repo / "f.txt").write_text(content)
    _git(repo, "commit", "-q", "-am", f"set {content.strip()}")


@pytest.fixture
def repo(tmp_path):
    """A base branch with one commit, and a PR branch cut from it."""
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.name", "t")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "commit.cleanup", "strip")
    (path / "f.txt").write_text("x\n")
    _git(path, "add", "f.txt")
    _git(path, "commit", "-q", "-m", "base")
    _git(path, "checkout", "-q", "-b", "feat/x")
    _fix(path, "y\n")
    return path, _git(path, "rev-parse", "HEAD").strip()


def _covered(path, head, **kw):
    kw.setdefault("round_number", 1)
    kw.setdefault("gate_lane", False)
    kw.setdefault("prior_heads", [])
    kw.setdefault("acceptance", None)
    kw.setdefault("round_started", T0 - timedelta(days=3650))
    return rr.covered_keys(str(path), head, **kw)


PREMISE_BLOCK = (
    "Design-premise: SOUND-BUT-INFERIOR\n"
    "Expected before checking: the cap holds\n"
    "P1 TRUE \u2014 the reader is bounded \u00b7 scripts/x.py:12 \u00b7 90% \u00b7 falsified by a huge file\n"
    "P2 FALSE \u2014 the caller retries \u00b7 scripts/y.py:3 \u00b7 80% \u00b7 falsified by a retry loop\n"
    "Effect: the gate refuses an oversize audit\n"
)


def _strong_premise(tmp_path: Path) -> Path:
    evidence = tmp_path / "premise.txt"
    evidence.write_text(PREMISE_BLOCK)
    return evidence


def _round_two(tmp_path: Path) -> list[str]:
    """What an ordinary round-2 reflection owes: the premise check."""
    return [*CHECK_LINES, f"Premise-evidence: {_strong_premise(tmp_path)} SOUND-BUT-INFERIOR"]


def _strong_audit(tmp_path: Path) -> Path:
    evidence = tmp_path / "audit.txt"
    evidence.write_text(
        "Severity ladder: BLOCKER none; SHOULD-FIX scripts/x.py:12 the loop is unbounded; "
        "NOTE scripts/y.py:3 a stale comment. Scope: the whole diff was read. " * 4
    )
    return evidence


def test_an_empty_reflection_on_the_head_covers_its_keys(repo, tmp_path):
    path, head = repo
    _commit_reflection(path, tmp_path, _reflection(head=head))
    assert _covered(path, head) == {"c1", "c2"}


def test_appended_trailers_keep_the_reflection_counted(repo, tmp_path):
    """This install's prepare-commit-msg appends Install: and Genesis-Session:
    after a blank line, which is prose to the grammar."""
    path, head = repo
    hook = path / ".git" / "hooks" / "prepare-commit-msg"
    hook.write_text(
        "#!/bin/sh\nprintf '\\nInstall: e05d97c0\\nGenesis-Session: d39f5cbc\\n' >> \"$1\"\n"
    )
    hook.chmod(0o755)
    _commit_reflection(path, tmp_path, _reflection(head=head))
    assert _covered(path, head) == {"c1", "c2"}


@pytest.mark.parametrize(
    "make, why",
    [
        (lambda h: f"Round-reflection: keys=c1,c2 head={h}\n", "a bare trailer"),
        (lambda h: _reflection(head="b" * 40), "another head"),
        (lambda h: _reflection(head=h, escalate="yes"), "it escalates"),
        (lambda h: _reflection(head=h, classes=()), "no class"),
    ],
)
def test_what_a_hand_made_reflection_cannot_cover(repo, tmp_path, make, why):
    path, head = repo
    _commit_reflection(path, tmp_path, make(head))
    assert _covered(path, head) == set(), why


def test_a_reflection_carrying_content_covers_nothing(repo, tmp_path):
    path, _ = repo
    head = _git(path, "rev-parse", "HEAD").strip()
    _commit_reflection(path, tmp_path, _reflection(head=head), empty=False)
    assert _covered(path, head) == set()


def test_a_reflection_made_after_a_fix_covers_nothing(repo, tmp_path):
    path, head = repo
    _fix(path, "fix\n")
    _commit_reflection(path, tmp_path, _reflection(head=head))
    assert _covered(path, head) == set()


def test_a_fix_reverted_before_the_reflection_still_disqualifies_it(repo, tmp_path):
    """Round 1 of #3055: the endpoint trees are equal, but a fix happened."""
    path, head = repo
    _fix(path, "fix\n")
    _fix(path, "y\n")
    _commit_reflection(path, tmp_path, _reflection(head=head))
    assert _covered(path, head) == set()


# -- the credit rule: a commit before a finding is no fix of it ----------------
#
# Owner rulings 2026-10-20: a merge of the default branch is no fix while it
# holds only git's own merge result and the resolution of its conflicted files
# (narrowed the same day), and any other commit between the round head
# and the reflection is a fix of finding k only when the LATER of its author
# and committer times is no earlier than k's review time (less the 1 s clock
# slack). Unknown means fix.

REVIEW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


def _dated(repo: Path, *args: str, when: datetime, committed: datetime | None = None) -> str:
    stamp = lambda t: t.strftime("%Y-%m-%dT%H:%M:%S+0000")  # noqa: E731
    env = {
        **os.environ,
        "GIT_AUTHOR_DATE": stamp(when),
        "GIT_COMMITTER_DATE": stamp(committed or when),
    }
    subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True, env=env)
    return _git(repo, "rev-parse", "HEAD").strip()


def _change(repo: Path, content: str, when: datetime, committed: datetime | None = None) -> str:
    (repo / "f.txt").write_text(content)
    return _dated(repo, "commit", "-q", "-am", f"set {content.strip()}", when=when, committed=committed)


def _main_moves(repo: Path, name: str = "main", *, remote: str | None = "main") -> None:
    """``name`` gains a commit; ``remote`` (if given) is the origin ref it is
    published as, so the merge reads as a merge of the default branch."""
    _git(repo, "checkout", "-q", name)
    (repo / "g.txt").write_text(f"{name}\n")
    _git(repo, "add", "g.txt")
    _git(repo, "commit", "-q", "-m", f"{name} moves")
    if remote:
        _git(repo, "update-ref", f"refs/remotes/origin/{remote}", name)
    _git(repo, "checkout", "-q", "feat/x")


def _at(minutes: int) -> datetime:
    return REVIEW + timedelta(minutes=minutes)


def test_a_commit_made_before_the_review_is_no_fix_of_it(repo, tmp_path):
    """The dead end this rule removes: a session that committed after pushing
    the head, before the head's review landed, could never reflect."""
    path, head = repo
    _change(path, "local\n", _at(-10))
    _commit_reflection(path, tmp_path, _reflection(head=head))
    assert _covered(path, head, reviewed_at={"c1": _at(0), "c2": _at(0)}) == {"c1", "c2"}
    # Control: with no review time the commit is a fix, as before.
    assert _covered(path, head) == set()


def test_a_commit_made_after_the_review_is_a_fix_of_it(repo, tmp_path):
    path, head = repo
    _change(path, "fix\n", _at(10))
    _commit_reflection(path, tmp_path, _reflection(head=head))
    assert _covered(path, head, reviewed_at={"c1": _at(0), "c2": _at(0)}) == set()


def test_each_key_is_judged_by_its_own_review_time(repo, tmp_path):
    path, head = repo
    _change(path, "between\n", _at(10))
    _commit_reflection(path, tmp_path, _reflection(head=head))
    assert _covered(path, head, reviewed_at={"c1": _at(0), "c2": _at(20)}) == {"c2"}


@pytest.mark.parametrize("seconds, credited", [(0, False), (-1, False), (-2, True)])
def test_the_clock_slack_counts_toward_a_fix(repo, tmp_path, seconds, credited):
    path, head = repo
    _change(path, "edge\n", REVIEW + timedelta(seconds=seconds))
    _commit_reflection(path, tmp_path, _reflection(keys=("c1",), head=head))
    got = _covered(path, head, reviewed_at={"c1": REVIEW})
    assert got == ({"c1"} if credited else set())


def test_an_amended_commit_is_judged_by_its_later_clock(repo, tmp_path):
    """An amend keeps the author time; the committer time moves, so a commit
    first written before the review and amended after it is a fix."""
    path, head = repo
    _change(path, "amended\n", _at(-10), committed=_at(10))
    _commit_reflection(path, tmp_path, _reflection(head=head))
    assert _covered(path, head, reviewed_at={"c1": _at(0), "c2": _at(0)}) == set()


def test_a_merge_of_the_default_branch_is_no_fix(repo, tmp_path):
    """Owner ruling 2026-10-20, replacing "any merge disqualifies": merging
    main in is how a branch keeps up, not how it fixes a finding."""
    path, head = repo
    _main_moves(path)
    _dated(path, "merge", "-q", "--no-edit", "main", when=_at(30))
    _commit_reflection(path, tmp_path, _reflection(head=head))
    assert _covered(path, head, reviewed_at={"c1": _at(0), "c2": _at(0)}) == {"c1", "c2"}
    # The main merge is exempt even when a key's review time is unknown.
    assert _covered(path, head) == {"c1", "c2"}


def _conflicting_main(repo: Path) -> None:
    """main and feat/x both change f.txt, so merging main in conflicts."""
    _git(repo, "checkout", "-q", "main")
    (repo / "f.txt").write_text("main's line\n")
    _git(repo, "commit", "-q", "-am", "main edits f")
    _git(repo, "update-ref", "refs/remotes/origin/main", "main")
    _git(repo, "checkout", "-q", "feat/x")
    done = subprocess.run(["git", "-C", str(repo), "merge", "-q", "main"], capture_output=True)
    assert done.returncode != 0, "the fixture must really conflict"


def test_a_conflict_resolved_main_merge_is_no_fix(repo, tmp_path):
    """The owner's accepted residual: what a resolution changes inside the
    conflicted files is credited, even after the review."""
    path, head = repo
    _conflicting_main(path)
    (path / "f.txt").write_text("resolved\n")
    _git(path, "add", "f.txt")
    _dated(path, "commit", "-q", "--no-edit", when=_at(30))
    _commit_reflection(path, tmp_path, _reflection(head=head))
    assert _covered(path, head, reviewed_at={"c1": _at(0), "c2": _at(0)}) == {"c1", "c2"}


def test_a_resolution_reaching_other_files_is_judged_by_its_time(repo, tmp_path):
    """Owner ruling 2026-10-20, narrowed: changes outside the conflicted files
    ride the merge as content (74 of 285 conflicted local main merges, 2026-10-20)."""
    path, head = repo
    (path / "h.txt").write_text("h\n")
    _git(path, "add", "h.txt")
    _dated(path, "commit", "-q", "-m", "add h", when=_at(-30))
    head = _git(path, "rev-parse", "HEAD").strip()
    _conflicting_main(path)
    (path / "f.txt").write_text("resolved\n")
    (path / "h.txt").write_text("a fix riding the merge\n")
    _git(path, "add", "f.txt", "h.txt")
    _dated(path, "commit", "-q", "--no-edit", when=_at(30))
    _commit_reflection(path, tmp_path, _reflection(head=head))
    assert _covered(path, head, reviewed_at={"c1": _at(0), "c2": _at(0)}) == set()
    assert _covered(path, head, reviewed_at={"c1": _at(40), "c2": _at(40)}) == {"c1", "c2"}


def _write(repo: Path, name: bytes, data: bytes) -> None:
    with open(bytes(repo) + b"/" + name, "wb") as f:
        f.write(data)


def _smuggling_merge(
    repo: Path, conflict: bytes, extra: bytes, *, gitlink: bool = False, gitmodules: str = ""
) -> str:
    """A main merge that resolves ``conflict`` and also edits ``extra``, a file
    git merged cleanly (or, with ``gitlink``, adds a submodule entry instead):
    content beyond the resolution. Returns the round head, the branch tip just
    before the merge."""
    _git(repo, "checkout", "-q", "main")
    for name in (conflict, extra):
        _write(repo, name, b"1\n2\n3\n4\n5\n")
    if gitmodules:
        (repo / ".gitmodules").write_text(gitmodules)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "add both files")
    _git(repo, "checkout", "-q", "feat/x")
    _git(repo, "merge", "-q", "--no-edit", "main")
    _git(repo, "checkout", "-q", "main")
    _write(repo, conflict, b"main\n2\n3\n4\n5\n")
    _write(repo, extra, b"1\n2\n3\n4\nmain\n")
    _git(repo, "commit", "-q", "-am", "main edits")
    _git(repo, "update-ref", "refs/remotes/origin/main", "main")
    _git(repo, "checkout", "-q", "feat/x")
    _write(repo, conflict, b"feat\n2\n3\n4\n5\n")
    _write(repo, extra, b"feat\n2\n3\n4\n5\n")
    _git(repo, "commit", "-q", "-am", "feat edits")
    head = _git(repo, "rev-parse", "HEAD").strip()
    done = subprocess.run(["git", "-C", str(repo), "merge", "-q", "main"], capture_output=True)
    assert done.returncode != 0, "the fixture must really conflict"
    _write(repo, conflict, b"resolved\n2\n3\n4\n5\n")
    if not gitlink:
        _write(repo, extra, b"feat\n2\nA FIX RIDING THE MERGE\n4\nmain\n")
    _git(repo, "add", "-A")
    if gitlink:  # after `add -A`, which would drop an entry with no directory
        _git(repo, "update-index", "--add", "--cacheinfo", f"160000,{'1' * 40},sub")
        assert "160000" in _git(repo, "ls-files", "-s", "sub")
    _dated(repo, "commit", "-q", "--no-edit", when=_at(30))
    return head


def _smuggled_is_a_fix(path: Path, tmp_path: Path, head: str, cwd: Path | None = None) -> None:
    _commit_reflection(path, tmp_path, _reflection(head=head))
    got = rr.covered_keys(
        str(cwd or path),
        head,
        round_number=1,
        gate_lane=False,
        prior_heads=[],
        acceptance=None,
        round_started=T0 - timedelta(days=3650),
        reviewed_at={"c1": _at(0), "c2": _at(0)},
    )
    assert got == set()


def test_the_smuggling_fixture_is_a_fix_with_no_tricks(repo, tmp_path):
    """Control for the four round-2 cases below: plain names, no config."""
    path, _ = repo
    _smuggled_is_a_fix(path, tmp_path, _smuggling_merge(path, b"c.txt", b"e.txt"))


def test_paths_that_are_not_utf8_cannot_hide_an_extra_edit(repo, tmp_path):
    """Round 2 of #3493 (Codex c4328462308): decoded with replacement, both
    names read as U+FFFD, so the extra edit looked like the conflicted file."""
    path, _ = repo
    _smuggled_is_a_fix(path, tmp_path, _smuggling_merge(path, b"\xff", b"\xfe"))


def test_an_ignored_submodule_change_still_counts(repo, tmp_path):
    """Round 2 of #3493 (Codex c4328462312): porcelain git diff honours
    diff.ignoreSubmodules=all and dropped a gitlink the merge carried."""
    path, _ = repo
    head = _smuggling_merge(path, b"c.txt", b"e.txt", gitlink=True)
    _git(path, "config", "diff.ignoreSubmodules", "all")
    _smuggled_is_a_fix(path, tmp_path, head)


def test_a_committed_submodule_ignore_cannot_hide_a_gitlink(repo, tmp_path):
    """Found while fixing round 2: a committed .gitmodules "ignore = all"
    hides a gitlink change from plumbing diff-tree too, not only from git diff."""
    path, _ = repo
    gitmodules = '[submodule "sub"]\n\tpath = sub\n\turl = ./sub\n\tignore = all\n'
    head = _smuggling_merge(path, b"c.txt", b"e.txt", gitlink=True, gitmodules=gitmodules)
    _smuggled_is_a_fix(path, tmp_path, head)


def test_a_repo_merge_default_cannot_widen_the_conflicted_set(repo, tmp_path):
    """Round-2 fix audit: merge.default=binary makes every file without a merge
    attribute conflict, so a fix inside a cleanly merging file was exempt."""
    path, _ = repo
    head = _smuggling_merge(path, b"c.txt", b"e.txt")
    _git(path, "config", "merge.default", "binary")
    _smuggled_is_a_fix(path, tmp_path, head)


def test_a_global_merge_default_cannot_widen_the_conflicted_set(repo, tmp_path, monkeypatch):
    """The same through the user's global ~/.gitconfig."""
    path, _ = repo
    head = _smuggling_merge(path, b"c.txt", b"e.txt")
    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text("[merge]\n\tdefault = binary\n")
    env = {k: v for k, v in rr._GIT_ENV.items() if k not in ("GIT_CONFIG_GLOBAL", "XDG_CONFIG_HOME")}
    monkeypatch.setattr(rr, "_GIT_ENV", {**env, "HOME": str(home)})
    _smuggled_is_a_fix(path, tmp_path, head)


def test_a_relative_diff_from_a_subdirectory_still_sees_the_whole_tree(repo, tmp_path):
    """The round-2 premise check's unreported member: diff.relative=true, with
    the check run from a subdirectory, limited porcelain diff to that directory."""
    path, _ = repo
    head = _smuggling_merge(path, b"c.txt", b"e.txt")
    (path / "sub").mkdir()
    _git(path, "config", "diff.relative", "true")
    _smuggled_is_a_fix(path, tmp_path, head, cwd=path / "sub")


def test_a_machine_attribute_cannot_widen_the_conflicted_set(repo, tmp_path):
    """The round-2 premise check's unreported member: core.attributesFile with
    `-merge` made a cleanly merging file conflict, so a fix inside it was
    exempt. (.git/info/attributes has no off switch: a stated residual.)"""
    path, _ = repo
    head = _smuggling_merge(path, b"c.txt", b"e.txt")
    attrs = tmp_path / "attributes"
    attrs.write_text("e.txt -merge\n")
    _git(path, "config", "core.attributesFile", str(attrs))
    _smuggled_is_a_fix(path, tmp_path, head)


def test_a_merge_tree_that_says_nothing_is_no_exemption(monkeypatch):
    """CodeRabbit c4328380094: a timed-out merge-tree reads as exit 1 with no
    output. It must not pass as a conflicted merge even if the next call
    happens to answer with nothing changed."""
    calls = []

    def fake(argv, cwd=None, *, env=None):
        calls.append(argv)
        return (1, b"", b"timed out") if "merge-tree" in argv else (0, b"", b"")

    monkeypatch.setattr(rr, "_run_bytes", fake)
    assert rr._merge_is_no_fix("/r", "t" * 40, ["a", "b"], {"b"}, {"a"}) is False
    assert len(calls) == 1


def test_the_merge_exemption_runs_git_with_injected_config_cut_off(monkeypatch):
    """A hook under `git -c ... commit` inherits that config through
    GIT_CONFIG_*: none of it, no system attributes and no grafts reach the
    exemption's git calls, and the global attributes file is cut too."""
    seen = []

    def fake(argv, cwd=None, *, env=None):
        seen.append((list(argv), env))
        return 1, b"", b""

    env = {
        "GIT_CONFIG_PARAMETERS": "'diff.ignoresubmodules'='all'",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "merge.default",
        "GIT_CONFIG_VALUE_0": "binary",
    }
    monkeypatch.setattr(rr, "_GIT_ENV", {**rr._GIT_ENV, **env})
    monkeypatch.setattr(rr, "_run_bytes", fake)
    rr._merge_is_no_fix("/r", "t" * 40, ["a", "b"], {"b"}, {"a"})
    argv, used = seen[0]
    assert not set(env) & set(used)
    assert used["GIT_CONFIG_GLOBAL"] == os.devnull and used["GIT_CONFIG_NOSYSTEM"] == "1"
    assert used["GIT_ATTR_NOSYSTEM"] == "1" and used["GIT_NO_REPLACE_OBJECTS"] == "1"
    assert not os.path.exists(used["GIT_GRAFT_FILE"])
    assert f"core.attributesFile={os.devnull}" in argv and "merge.default=text" in argv


def test_a_clean_main_merge_with_extra_changes_is_judged_by_its_time(repo, tmp_path):
    """An edited merge (30 of 494 local main merges, 2026-10-20) is content."""
    path, head = repo
    _main_moves(path)
    _git(path, "merge", "-q", "--no-commit", "--no-ff", "main")
    (path / "f.txt").write_text("a fix in the merge\n")
    _git(path, "add", "f.txt")
    _dated(path, "commit", "-q", "--no-edit", when=_at(30))
    _commit_reflection(path, tmp_path, _reflection(head=head))
    assert _covered(path, head, reviewed_at={"c1": _at(0), "c2": _at(0)}) == set()
    assert _covered(path, head, reviewed_at={"c1": _at(40), "c2": _at(40)}) == {"c1", "c2"}


def test_a_main_merge_amended_with_a_fix_is_judged_by_its_time(repo, tmp_path):
    path, head = repo
    _main_moves(path)
    _dated(path, "merge", "-q", "--no-edit", "main", when=_at(10))
    (path / "f.txt").write_text("amended in\n")
    _dated(path, "commit", "-q", "-a", "--amend", "--no-edit", when=_at(10), committed=_at(30))
    _commit_reflection(path, tmp_path, _reflection(head=head))
    assert _covered(path, head, reviewed_at={"c1": _at(0), "c2": _at(0)}) == set()


def test_a_merge_that_brings_nothing_new_is_judged_by_its_time(repo, tmp_path):
    """A 'merge' of a main commit the branch already holds adds no main
    content, so whatever its tree holds is the session's own."""
    path, head = repo
    _git(path, "update-ref", "refs/remotes/origin/main", "main")
    fixed = _change(path, "fixed\n", _at(-50))
    _git(path, "reset", "-q", "--hard", head)
    tree = _git(path, "rev-parse", f"{fixed}^{{tree}}").strip()
    base = _git(path, "merge-base", "HEAD", "main").strip()
    env = {**os.environ, "GIT_AUTHOR_DATE": "2026-10-05T12:30:00+0000"}
    env["GIT_COMMITTER_DATE"] = env["GIT_AUTHOR_DATE"]
    made = subprocess.run(
        ["git", "-C", str(path), "commit-tree", tree, "-p", "HEAD", "-p", base, "-m", "m"],
        capture_output=True, text=True, check=True, env=env,
    ).stdout.strip()
    _git(path, "reset", "-q", "--hard", made)
    _commit_reflection(path, tmp_path, _reflection(head=head))
    assert _covered(path, head, reviewed_at={"c1": _at(0), "c2": _at(0)}) == set()


def test_a_topic_later_merged_into_main_is_still_judged_by_its_time(repo, tmp_path):
    """Round 1 of #3493 (Codex c4326223553): the branch merges a topic after
    the review; main later merges that same topic with a merge commit, so the
    topic becomes reachable from main through a SECOND parent. It was never
    main's tip, so the earlier merge is still the branch's own content."""
    path, head = repo
    _git(path, "checkout", "-q", "-b", "topic", "main")
    (path / "t.txt").write_text("the fix\n")
    _git(path, "add", "t.txt")
    _git(path, "commit", "-q", "-m", "topic")
    _git(path, "checkout", "-q", "feat/x")
    _dated(path, "merge", "-q", "--no-ff", "--no-edit", "topic", when=_at(10))
    _commit_reflection(path, tmp_path, _reflection(head=head))
    _git(path, "checkout", "-q", "main")
    _git(path, "commit", "-q", "--allow-empty", "-m", "main moves")
    _git(path, "merge", "-q", "--no-ff", "--no-edit", "topic")
    _git(path, "update-ref", "refs/remotes/origin/main", "main")
    _git(path, "checkout", "-q", "feat/x")
    topic = _git(path, "rev-parse", "topic").strip()
    done = subprocess.run(["git", "-C", str(path), "merge-base", "--is-ancestor", topic, "main"])
    assert done.returncode == 0, "the fixture must put the topic in main"
    assert _covered(path, head, reviewed_at={"c1": _at(0), "c2": _at(0)}) == set()


def test_a_fix_cannot_ride_in_through_a_merges_first_parent(repo, tmp_path):
    """Fix-code audit N1: when the head is already in main, a branch forked
    elsewhere can merge main in and reach the reflection with its fix on the
    merge's FIRST parent, off the head's line. That merge is no main merge."""
    path, head = repo
    _git(path, "checkout", "-q", "main")
    _dated(path, "merge", "-q", "--no-ff", "--no-edit", "feat/x", when=_at(-30))
    _git(path, "update-ref", "refs/remotes/origin/main", "main")
    base = _git(path, "rev-parse", "main^1").strip()
    _git(path, "checkout", "-q", "-b", "fixside", base)
    (path / "fix.txt").write_text("the fix\n")
    _git(path, "add", "fix.txt")
    _dated(path, "commit", "-q", "-m", "the fix", when=_at(30))
    _dated(path, "merge", "-q", "--no-edit", "origin/main", when=_at(40))
    _commit_reflection(path, tmp_path, _reflection(head=head))
    assert _covered(path, head, reviewed_at={"c1": _at(0), "c2": _at(0)}) == set()


def test_an_unfetched_head_is_unknown_on_the_open_path(repo, tmp_path):
    """Fix-code audit NOTE 1: a round head this clone lacks refuses (status
    reports it could not be checked); it never reads as every key owed or as
    nothing owed."""
    path, head = repo
    _commit_reflection(path, tmp_path, _reflection(head="e" * 40))
    with pytest.raises(rr.Refused, match="not fetched"):
        _covered(path, "e" * 40)


def test_a_dangling_remote_head_falls_back_to_origin_main(repo, tmp_path):
    """After a default-branch rename and prune, origin/HEAD can name a ref
    that is gone: that must neither refuse the status nor drop the rule."""
    path, head = repo
    _main_moves(path)
    _git(path, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/gone")
    _dated(path, "merge", "-q", "--no-edit", "main", when=_at(30))
    _commit_reflection(path, tmp_path, _reflection(head=head))
    assert _covered(path, head, reviewed_at={"c1": _at(0), "c2": _at(0)}) == {"c1", "c2"}


def test_a_parentless_commit_after_the_head_credits_nothing(repo, tmp_path, monkeypatch):
    """Only the local commit loses its parent; the reflection itself (which
    ``head..sha`` also lists) keeps its own, so it is still no fix."""
    path, head = repo
    local = _change(path, "local\n", _at(-10))
    _commit_reflection(path, tmp_path, _reflection(head=head))
    assert _covered(path, head, reviewed_at={"c1": _at(0), "c2": _at(0)}) == {"c1", "c2"}
    real = rr._run

    def orphan(argv, cwd=None):
        code, out, err = real(argv, cwd)
        if "--ancestry-path" in argv:
            rows = []
            for row in (r for r in out.split("\n") if r.strip()):
                names, _, stamps = row.partition("|")
                if names.startswith(local):
                    names = " ".join(names.split()[:2])
                rows.append(f"{names}|{stamps}")
            out = "\n".join(rows)
        return code, out, err

    monkeypatch.setattr(rr, "_run", orphan)
    assert _covered(path, head, reviewed_at={"c1": _at(0), "c2": _at(0)}) == set()


def test_a_merge_of_another_branch_is_judged_by_its_time(repo, tmp_path):
    """Only the one-parent rule used to decide this: `-s ours` keeps the tree.
    A merge of anything but the default branch is a fix after the review and
    no fix before it."""
    path, head = repo
    _git(path, "checkout", "-q", "-b", "side", "main")
    _git(path, "commit", "-q", "--allow-empty", "-m", "side")
    _git(path, "update-ref", "refs/remotes/origin/main", "main")
    _git(path, "checkout", "-q", "feat/x")
    _dated(path, "merge", "-q", "--no-edit", "-s", "ours", "side", when=_at(10))
    assert _git(path, "rev-parse", "HEAD^{tree}") == _git(path, "rev-parse", "HEAD^1^{tree}")
    _commit_reflection(path, tmp_path, _reflection(head=head))
    assert _covered(path, head, reviewed_at={"c1": _at(0), "c2": _at(0)}) == set()
    assert _covered(path, head, reviewed_at={"c1": _at(20), "c2": _at(20)}) == {"c1", "c2"}


def test_with_no_known_default_branch_a_merge_is_judged_by_its_time(repo, tmp_path):
    path, head = repo
    _main_moves(path, remote=None)
    _dated(path, "merge", "-q", "--no-edit", "main", when=_at(10))
    _commit_reflection(path, tmp_path, _reflection(head=head))
    assert _covered(path, head, reviewed_at={"c1": _at(0), "c2": _at(0)}) == set()
    assert _covered(path, head, reviewed_at={"c1": _at(20), "c2": _at(20)}) == {"c1", "c2"}


def test_the_remote_head_names_the_default_branch(repo, tmp_path):
    """``origin/HEAD`` wins over ``origin/main`` when it is set."""
    path, head = repo
    _git(path, "branch", "trunk", "main")
    _main_moves(path, "trunk", remote="trunk")
    _git(path, "update-ref", "refs/remotes/origin/main", "main")
    _git(path, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/trunk")
    _dated(path, "merge", "-q", "--no-edit", "trunk", when=_at(10))
    _commit_reflection(path, tmp_path, _reflection(head=head))
    assert _covered(path, head, reviewed_at={"c1": _at(0), "c2": _at(0)}) == {"c1", "c2"}


def test_an_unreadable_commit_time_is_a_fix(repo, tmp_path, monkeypatch):
    path, head = repo
    _change(path, "local\n", _at(-10))
    _commit_reflection(path, tmp_path, _reflection(head=head))
    real = rr._run

    def blank_times(argv, cwd=None):
        code, out, err = real(argv, cwd)
        if "--ancestry-path" in argv:
            out = "\n".join(row.split("|")[0] + "|" for row in out.split("\n") if row.strip())
        return code, out, err

    monkeypatch.setattr(rr, "_run", blank_times)
    assert _covered(path, head, reviewed_at={"c1": _at(0), "c2": _at(0)}) == set()


def test_a_reflection_not_descending_from_the_head_credits_nothing(repo, tmp_path):
    path, head = repo
    _git(path, "checkout", "-q", "-b", "elsewhere", "main")
    _commit_reflection(path, tmp_path, _reflection(head=head))
    assert _covered(path, head, reviewed_at={"c1": _at(0), "c2": _at(0)}) == set()


def test_fix_clock_and_clears_state_the_rule(repo, tmp_path):
    path, head = repo
    local = _at(-10)
    _change(path, "local\n", local)
    sha = _commit_reflection(path, tmp_path, _reflection(head=head))
    clock = rr.fix_clock(str(path), head, sha)
    assert clock == local
    assert rr.clears(clock, _at(0))
    assert not rr.clears(clock, _at(-20))
    assert not rr.clears(clock, None)
    assert rr.clears(None, None)
    assert not rr.clears(rr.NEVER, _at(0))


def test_an_expired_deadline_refuses(repo, tmp_path):
    """The commit gate passes its own remaining time: running out is unknown,
    never "nothing owed" and never a kill that fails open by accident."""
    path, head = repo
    _commit_reflection(path, tmp_path, _reflection(head=head))
    with pytest.raises(rr.Refused, match="ran out of time"):
        _covered(path, head, deadline=0.0)
    assert _covered(path, head, deadline=None) == {"c1", "c2"}


def test_git_under_a_deadline_is_bounded_by_it(monkeypatch):
    seen = {}

    def fake(argv, **kw):
        seen["timeout"] = kw["timeout"]
        raise subprocess.TimeoutExpired(argv, kw["timeout"])

    monkeypatch.setattr(rr.subprocess, "run", fake)
    import time

    with rr.deadline_scope(time.monotonic() + 5), pytest.raises(rr.Refused, match="ran out"):
        rr._run(["git", "status"])
    assert 0 < seen["timeout"] <= 5
    # With no deadline a timeout is an ordinary failed command, as before.
    assert rr._run(["git", "status"])[0] == 1 and seen["timeout"] == 120


def test_a_second_reflection_for_the_same_round_still_counts(repo, tmp_path):
    path, head = repo
    _commit_reflection(path, tmp_path, _reflection(keys=("c1",), head=head))
    _commit_reflection(path, tmp_path, _reflection(keys=("c9",), head=head))
    assert _covered(path, head) == {"c1", "c9"}


def test_a_hand_made_reflection_meets_the_recurring_class_rule(repo, tmp_path):
    """Re-audit S1, with a control that only the recurring-class rule decides."""
    path, first = repo
    _commit_reflection(path, tmp_path, _reflection(head=first))
    _fix(path, "fix\n")
    second = _git(path, "rev-parse", "HEAD").strip()
    owed = _round_two(tmp_path)
    sound = "SOUND-BUT-INFERIOR"
    repeat = _reflection(head=second, premise=sound, decision="fix-instances", extra=owed)
    _commit_reflection(path, tmp_path, repeat)
    assert _covered(path, second, round_number=2, prior_heads=[first]) == set()
    closing = _reflection(head=second, premise=sound, decision="close-class", extra=owed)
    _commit_reflection(path, tmp_path, closing)
    assert _covered(path, second, round_number=2, prior_heads=[first]) == {"c1", "c2"}


def test_a_stray_reflection_cannot_stand_in_for_the_previous_round(repo, tmp_path):
    """Fix-code audit S1 of #3055: a reflection naming a head that is not one
    of this PR's earlier round heads (mistyped, stale) is ignored, so it can
    neither hide nor replace the real previous round's classes."""
    path, first = repo
    _commit_reflection(path, tmp_path, _reflection(head=first))
    _fix(path, "fix\n")
    second = _git(path, "rev-parse", "HEAD").strip()
    decoy = _reflection(head="0" * 40, classes=("decoy class = 1",))
    _commit_reflection(path, tmp_path, decoy)
    assert rr.previous_class_labels(str(path), [first]) == ["unchecked subprocess result"]
    owed = _round_two(tmp_path)
    sound = "SOUND-BUT-INFERIOR"
    repeat = _reflection(head=second, premise=sound, decision="fix-instances", extra=owed)
    _commit_reflection(path, tmp_path, repeat)
    assert _covered(path, second, round_number=2, prior_heads=[first]) == set()
    # Control: the same reflection deciding close-class IS covered, so the
    # refusal above came from the recurring-class rule and nothing else.
    closing = _reflection(head=second, premise=sound, decision="close-class", extra=owed)
    _commit_reflection(path, tmp_path, closing)
    assert _covered(path, second, round_number=2, prior_heads=[first]) == {"c1", "c2"}


def test_a_class_from_any_earlier_round_recurs(repo, tmp_path):
    """Fix-code audit S1: absent in round 2 and back in round 3 still recurs."""
    path, first = repo
    _commit_reflection(path, tmp_path, _reflection(head=first, classes=("alpha = 1",)))
    _fix(path, "two\n")
    second = _git(path, "rev-parse", "HEAD").strip()
    _commit_reflection(path, tmp_path, _reflection(head=second, classes=("beta = 1",)))
    assert sorted(rr.previous_class_labels(str(path), [first, second])) == ["alpha", "beta"]


def test_an_invalid_or_content_reflection_adds_no_previous_class(repo, tmp_path):
    path, first = repo
    broken = _reflection(head=first, classes=("ghost = 1",), escalate="maybe")
    _commit_reflection(path, tmp_path, broken)
    # Round 2 of #3055: an escalating draft parses, but was never an answer.
    draft = _reflection(head=first, classes=("draft = 1",), escalate="yes")
    _commit_reflection(path, tmp_path, draft)
    content = _reflection(head=first, classes=("loaded = 1",))
    _commit_reflection(path, tmp_path, content, empty=False)
    assert rr.previous_class_labels(str(path), [first]) == []


def test_a_control_byte_in_the_prose_cannot_hide_an_escalation(repo, tmp_path):
    """Fix-code audit S2: records were split on a control byte a message may
    hold, so everything after it was dropped from the escalation search."""
    path, head = repo
    text = _reflection(head=head, prose="notes \x01 Escalate: yes")
    assert rr.parse(text).escalate
    _commit_reflection(path, tmp_path, text)
    assert "\x01" in _git(path, "log", "-1", "--format=%B")
    assert _covered(path, head) == set()


def test_a_reflection_on_another_line_of_history_covers_nothing(repo, tmp_path):
    """The ancestry guard: head on a side branch, the reflection elsewhere;
    head..reflection is just the empty reflection, so only the guard decides."""
    path, _ = repo
    _git(path, "checkout", "-q", "-b", "side")
    _fix(path, "side\n")
    side_head = _git(path, "rev-parse", "HEAD").strip()
    _git(path, "checkout", "-q", "feat/x")
    _commit_reflection(path, tmp_path, _reflection(head=side_head))
    assert _covered(path, side_head) == set()


def test_a_rebase_cannot_move_the_audit_bound_forward(repo, tmp_path):
    """Fix-code audit S1 of round 2: a rebase rewrites the committer time, so
    the bound is the earlier of author and committer time."""
    path, head = repo
    evidence = tmp_path / "late-audit.txt"
    cited = _reflection(head=head, extra=[f"Audit-evidence: {evidence} PASS"])
    _commit_reflection(path, tmp_path, cited)
    authored = int(_git(path, "log", "-1", "--format=%at").strip())
    evidence.write_text(_strong_audit(tmp_path).read_text())
    os.utime(evidence, (authored + 30, authored + 30))
    env = {**os.environ, "GIT_COMMITTER_DATE": f"{authored + 60} +0000"}
    subprocess.run(
        ["git", "-C", str(path), "commit", "-q", "--amend", "--allow-empty", "--no-edit"],
        env=env,
        check=True,
    )
    assert int(_git(path, "log", "-1", "--format=%ct").strip()) == authored + 60
    assert _covered(path, head, gate_lane=True) == set()


@pytest.mark.parametrize(
    "line, want",
    [
        (b"committer t <t@example.com> 1791417497 -0400", 1791417497),
        (b"committer a name with spaces <t@example.com> 1791417497 +0000", 1791417497),
        (b"committer t <t@example.com> 300000000000 +0000", None),
        (b"committer t <t@example.com> notatime +0000", None),
        (b"committer", None),
    ],
)
def test_header_times_parse_or_are_unknown_never_a_crash(line, want):
    got = rr._header_time(line)
    assert (got.timestamp() if got else None) == want


def test_the_reflection_read_ignores_the_grep_pattern_setting(repo, tmp_path):
    """Fix-code audit N1: grep.patternType=fixed read ^ literally."""
    path, head = repo
    _git(path, "config", "grep.patternType", "fixed")
    _commit_reflection(path, tmp_path, _reflection(head=head))
    assert _covered(path, head) == {"c1", "c2"}


def test_a_history_read_that_reaches_its_cap_refuses(repo, tmp_path, monkeypatch):
    """Round 2 of #3055: a capped read silently dropped the oldest
    reflections, and with them earlier rounds' classes."""
    path, head = repo
    monkeypatch.setattr(rr, "LOG_DEPTH", 2)
    _commit_reflection(path, tmp_path, _reflection(head=head))
    assert _covered(path, head) == {"c1", "c2"}
    _commit_reflection(path, tmp_path, _reflection(head=head))
    with pytest.raises(rr.Refused, match="cut off"):
        _covered(path, head)


def test_only_reflection_commits_count_toward_the_cap(repo, tmp_path, monkeypatch):
    path, head = repo
    monkeypatch.setattr(rr, "LOG_DEPTH", 2)
    for n in range(5):
        _git(path, "commit", "-q", "--allow-empty", "-m", f"ordinary {n}")
    _commit_reflection(path, tmp_path, _reflection(head=head))
    assert rr.previous_class_labels(str(path), [head]) == ["unchecked subprocess result"]


def test_git_replace_cannot_swap_out_a_fix(repo, tmp_path):
    """Fix-code audit N1: reads ignore replace objects."""
    path, head = repo
    _fix(path, "fix\n")
    reflection = _commit_reflection(path, tmp_path, _reflection(head=head))
    message = _git(path, "log", "-1", "--format=%B", reflection)
    tree = _git(path, "rev-parse", f"{head}^{{tree}}").strip()
    forged = subprocess.run(
        ["git", "-C", str(path), "commit-tree", tree, "-p", head, "-F", "-"],
        input=message,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    _git(path, "replace", reflection, forged)
    assert _covered(path, head) == set()


def test_the_previous_round_combines_every_reflection_on_its_head(repo, tmp_path):
    """Round 1 of #3055: a late review's second reflection on the previous
    head carries classes too; the newest alone is not the previous round."""
    path, first = repo
    one = _reflection(keys=("c1",), head=first, classes=("alpha = 1",))
    two = _reflection(keys=("c9",), head=first, classes=("beta = 1",))
    _commit_reflection(path, tmp_path, one)
    _commit_reflection(path, tmp_path, two)
    _fix(path, "fix\n")
    labels = rr.previous_class_labels(str(path), [first])
    assert sorted(labels) == ["alpha", "beta"]


def test_previous_classes_come_only_from_the_named_earlier_heads(repo, tmp_path):
    """A stacked base branch's reflections and this round's own never count:
    only reflections naming one of the PR's earlier round heads do."""
    path, head = repo
    _git(path, "checkout", "-q", "main")
    _commit_reflection(path, tmp_path, _reflection(head="c" * 40, classes=("base class = 1",)))
    _git(path, "checkout", "-q", "feat/x")
    _git(path, "rebase", "-q", "main")
    head = _git(path, "rev-parse", "HEAD").strip()
    assert rr.previous_class_labels(str(path), [head]) == []
    _commit_reflection(path, tmp_path, _reflection(head="b" * 40, classes=("old class = 1",)))
    _commit_reflection(path, tmp_path, _reflection(head=head, classes=("this round = 1",)))
    assert rr.previous_class_labels(str(path), ["b" * 40]) == ["old class"]


def test_cited_audit_evidence_is_checked(repo, tmp_path):
    path, head = repo
    missing = _reflection(head=head, extra=["Audit-evidence: ~/.genesis/no-such-audit.txt PASS"])
    _commit_reflection(path, tmp_path, missing)
    assert _covered(path, head, gate_lane=True) == set()
    strong = _strong_audit(tmp_path)
    cited = _reflection(head=head, extra=[f"Audit-evidence: {strong} PASS"])
    _commit_reflection(path, tmp_path, cited)
    assert _covered(path, head, gate_lane=True) == {"c1", "c2"}
    later = datetime.now(UTC) + timedelta(hours=1)
    assert _covered(path, head, gate_lane=True, round_started=later) == set()
    assert _covered(path, head, gate_lane=True, round_started="not a time") == set()
    assert _covered(path, head, gate_lane=True, round_started=None) == set()


def test_undecodable_audit_evidence_is_unreadable_not_a_crash(tmp_path):
    """Round 1 of #3055: invalid UTF-8 evidence must refuse the reflection."""
    bad = tmp_path / "bad.txt"
    bad.write_bytes(b"\xff\xfe not utf-8 \xc3")
    assert "not valid UTF-8" in rr.audit_problem(str(bad), T0, T0, cwd=str(tmp_path))


def test_an_audit_with_no_known_commit_time_is_refused(tmp_path):
    strong = _strong_audit(tmp_path)
    cwd = str(tmp_path)
    assert "commit time is unknown" in rr.audit_problem(str(strong), T0, None, cwd=cwd)
    later = datetime.now(UTC) + timedelta(hours=1)
    assert rr.audit_problem(str(strong), T0, later, cwd=cwd) is None


# -- the evidence file itself (#3089 items 2, 4 and 5) --------------------------


def _evidence(path, *, kind="audit", cwd, made=None):
    made = made or datetime.now(UTC) + timedelta(hours=1)
    return rr.evidence_problem(str(path), kind=kind, cwd=str(cwd), round_started=T0, made_at=made)


def test_a_pipe_cited_as_evidence_is_refused_not_a_hang(tmp_path):
    """#3089 item 4: read_bytes() on a FIFO blocked forever. The type is taken
    from the opened descriptor, so this returns at once."""
    import threading

    fifo = tmp_path / "audit.fifo"
    os.mkfifo(fifo)
    got: list[object] = []
    worker = threading.Thread(target=lambda: got.append(_evidence(fifo, cwd=tmp_path)))
    worker.daemon = True
    worker.start()
    worker.join(5)
    if worker.is_alive():
        # Unblock the stuck open so the daemon thread can finish.
        os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
        pytest.fail("reading a FIFO blocked: the open must not wait for a writer")
    assert "not a regular file" in str(got[0])


def test_a_directory_cited_as_evidence_is_refused(tmp_path):
    assert "not a regular file" in _evidence(tmp_path, cwd=tmp_path)


def test_oversize_evidence_is_refused(tmp_path):
    big = tmp_path / "big.txt"
    big.write_bytes(b"x" * (rr.EVIDENCE_MAX_BYTES + 1))
    assert "larger than" in _evidence(big, cwd=tmp_path)


def test_evidence_exactly_at_the_cap_is_read(tmp_path):
    exact = tmp_path / "exact.txt"
    exact.write_bytes(b"x" * rr.EVIDENCE_MAX_BYTES)
    got = _evidence(exact, cwd=tmp_path)
    assert "larger than" not in got and "not an adversarial audit" in got


def test_a_relative_path_is_read_from_the_repository_not_the_process(tmp_path, monkeypatch):
    """#3089 item 5: a gate runs from wherever its process happens to be."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    (repo_dir / "audit.txt").write_text(_strong_audit(tmp_path).read_text())
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert _evidence("audit.txt", cwd=repo_dir) is None
    assert "unreadable" in _evidence("audit.txt", cwd=elsewhere)


def test_the_commit_clock_slack_is_one_second(tmp_path):
    """#3089 item 2: a file written in the commit's own second may carry a
    later fractional mtime than the whole-second commit time."""
    audit = _strong_audit(tmp_path)
    made = datetime(2026, 10, 7, 12, 30, tzinfo=UTC)
    stamp = made.timestamp()
    os.utime(audit, (stamp + 0.9, stamp + 0.9))
    assert _evidence(audit, cwd=tmp_path, made=made) is None
    os.utime(audit, (stamp + 1.5, stamp + 1.5))
    assert "after the reflection was committed" in _evidence(audit, cwd=tmp_path, made=made)


#: The block as #3107 shipped it: verdict last. The canonical block (#3120)
#: no longer reads it.
OLD_PREMISE_BLOCK = (
    "Design-premise: SOUND-BUT-INFERIOR\n"
    "Expected before checking: the cap holds\n"
    "  P1 the reader is bounded — TRUE · scripts/x.py:12\n"
    "  P2 the caller retries — FALSE · scripts/y.py:3\n"
)


EXPECTED = "Expected before checking: it holds"


def _block(head="Design-premise: SOUND", *claims, between=(EXPECTED,)):
    """A canonical block: the verdict line, the required Expected line (#3238),
    then the claims. Pass ``between=()`` for a block without the Expected line."""
    claims = claims or ("P1 TRUE — a", "P2 FALSE — b")
    return "\n".join([head, *between, *claims]) + "\n"


@pytest.mark.parametrize(
    "text, ok",
    [
        (PREMISE_BLOCK, True),
        (OLD_PREMISE_BLOCK, False),  # verdict last: the shape #3107 shipped
        (PREMISE_BLOCK.replace("Design-premise", "Premise"), False),  # no verdict line
        (_block("Design-premise: SOUND", "P1 TRUE — a"), False),  # one claim
        (_block("Design-premise: SOUND", "P1 TRUE — a", "P1 TRUE — b"), False),  # one distinct
        (_block("Design-premise: SOUND", "P1 TRUE — a", "P1 FALSE — b", "P2 TRUE \u2014 c"), False),
        ("Premise: SOUND (~80%)\n| 1 | real | TRUE |\n| 2 | real | FALSE |\n", False),  # summary
        ("**Design-premise:** SOUND\n- P1 TRUE — a\n- P2 FALSE — b\n", False),  # markdown
        ("Design-premise: BROKEN\n| P1 | TRUE | a |\n| P2 | FALSE | b |\n", False),  # a table
        ("  Design-premise: SOUND\n  P1 TRUE — a\n  P2 FALSE — b\n", False),  # quoted, indented
        ("> Design-premise: SOUND\n> P1 TRUE — a\n> P2 FALSE — b\n", False),  # quoted, >
        (_block("Design-premise: SOUND", "P1 TRUE — a", "", "P2 FALSE — b"), False),  # gap
        (_block("Design-premise: SOUND — UNPROVEN(1)", "P1 TRUE — a", "P2 UNPROVEN — b"), True),
        (_block("Design-premise: BROKEN — premises hold, effect is nil"), True),
        (_block(between=("Expected before checking: it holds",)), True),
        (_block(between=("Expected before checking: it", "holds")), False),  # wrapped Expected
        (_block("Design-premise: SOUND", "  P1 TRUE — a", "\tP2 FALSE — b"), True),
        # #3127 round 1: the separator and the claim body are required
        (_block("Design-premise: SOUND", "P1: TRUE \u2014 a", "P2 FALSE \u2014 b"), False),
        (_block("Design-premise: SOUND", "P1. TRUE \u2014 a", "P2 FALSE \u2014 b"), False),
        (_block("Design-premise: SOUND", "P1 TRUE a", "P2 FALSE \u2014 b"), False),
        (_block("Design-premise: SOUND", "P1 TRUE \u2014 a", "P2 FALSE"), False),
        (_block("Design-premise: SOUND", "P1 TRUE \u2014 a", "P2 FALSE \u2014"), False),
        (_block("Design-premise: SOUND", "P1 TRUE \u2014 a", "P2  FALSE \u2014 b"), False),
        (_block("Design-premise: SOUND", "P1 TRUE \u2212 a", "P2 FALSE \u2014 b"), False),
        (_block("Design-premise: SOUND", "P1 TRUE \u2013 a", "P2 FALSE - b"), True),
        (_block("Design-premise: SOUND", "P1 TRUE \u2014 a", "P2 FALSE (partially) \u2014 b"), True),
        (_block(between=("Expected before checking:",)), False),  # empty Expected
        (_block(between=("Expected before checking:   ",)), False),
        (
            "Design-premise: SOUND  \r\nExpected before checking: x \r\nP1 TRUE — a \r\nP2 FALSE — b\r\n",
            True,
        ),  # CRLF
        (_block(between=()), False),  # #3238: the Expected line is required
        ("Some prose first.\n\n```\n" + _block() + "```\n\nEffect: none\n", True),  # fenced
    ],
)
def test_premise_evidence_must_be_a_premise_check_block(tmp_path, text, ok):
    """The canonical block of .claude/docs/premise-check.md (#3120): a verdict
    line at column 0, then at least two distinct ``P<n> <verdict>`` claims,
    verdict first, directly under it. Markdown, tables, a quoted block and a
    hand summary are refused (MEASURED 2026-10-08: a summarised premise-check
    file on disk had neither)."""
    evidence = tmp_path / "premise.txt"
    evidence.write_bytes(text.encode())
    got = _evidence(evidence, kind="premise", cwd=tmp_path)
    assert (got is None) is ok, got


@pytest.mark.parametrize(
    "line, verdict",
    [
        ("Design-premise: SOUND-BUT-INFERIOR", "SOUND-BUT-INFERIOR"),
        ("Design-premise: SOUND", "SOUND"),
        ("Design-premise: BROKEN", "BROKEN"),
        ("Design-premise: SOUND — UNPROVEN(1)", "SOUND"),
        ("Design-premise: SOUND - UNPROVEN(1)", "SOUND"),
        ("Design-premise: [SOUND / SOUND-BUT-INFERIOR / BROKEN]", None),  # the template
        ("Design-premise: SOUND|SOUND-BUT-INFERIOR|BROKEN", None),
        ("Design-premise: SOUNDNESS", None),
        ("Design-premise: SOUND/BROKEN", None),
        ("Design-premise: SOUND / BROKEN", None),
        ("Design-premise: SOUND-BUT", None),
        ("Design-premise: SOUND (80%)", None),  # a qualifier needs its dash
        ("**Design-premise: BROKEN**", None),
        ("| Design-premise: SOUND |", None),
        ("later: Design-premise: SOUND", None),
    ],
)
def test_the_premise_verdict_is_read_whole(line, verdict):
    """Class audit C1c: the verdict is the whole token in its slot, never a
    prefix of one, a template or a line that only mentions it."""
    got, claims, why = rr.premise_block(_block(line))
    assert got == verdict, why
    if verdict:
        assert claims == {"1": "TRUE", "2": "FALSE"}


@pytest.mark.parametrize(
    "line, verdict",
    [
        ("P1 TRUE — whether FALSE values survive · falsified by an UNPROVEN read", "TRUE"),
        ("P1 FALSE — TRUE rows only — FALSE", "FALSE"),  # #3120's N3 residual
        ("P1 UNPROVEN \u2014 TRUE cases are counted", "UNPROVEN"),
        ("P1 UNPROVEN TRUE cases are counted", None),  # no dash: refused by line
        ("P1 FALSE (only TRUE rows) \u2014 a", "FALSE"),
        ("P1 whether TRUE values survive — FALSE", None),  # verdict last: not a claim
        ("P1 TRUEISH — a", None),
        ("P1 true — a", None),
        ("P1TRUE — a", None),
        ("- P1 TRUE — a", None),
    ],
)
def test_a_claim_verdict_is_the_word_in_its_slot(line, verdict):
    """#3107 round 2 c4225376510 and #3120: a verdict word inside the claim
    text was read as the claim's verdict. The verdict is the token right after
    the claim number, and nothing later on the line can change it."""
    got = rr.premise_block(_block("Design-premise: SOUND", line, "P2 TRUE — b"))
    assert got[1].get("1") == verdict


def test_claim_lines_outside_the_block_are_prose():
    """Only the run directly under a verdict line is the claims; the run ends
    at the first line that is not a claim."""
    text = _block() + "\nLater, P1 FALSE was considered and rejected.\nP3 FALSE — stray\n"
    assert rr.premise_block(text)[1] == {"1": "TRUE", "2": "FALSE"}


def test_a_misstated_claim_is_refused_when_its_text_names_a_verdict(tmp_path):
    evidence = tmp_path / "premise.txt"
    evidence.write_text(
        "Design-premise: SOUND\n"
        "Expected before checking: the values survive\n"
        "P1 FALSE — whether TRUE values survive · x.py:1\n"
        "P2 TRUE — the caller retries · y.py:3\n"
    )

    def problem(p1):
        lines = [
            f"Premise-check: P1 {p1} whether the stored values survive",
            "Premise-check: P2 TRUE the caller retries on a refusal",
            f"Premise-evidence: {evidence} SOUND",
        ]
        parsed = rr.parse(_reflection(premise="SOUND", extra=lines), round_number=2)
        assert parsed.ok, parsed.problems
        return rr.evidence_problem(
            str(evidence),
            kind="premise",
            cwd=str(tmp_path),
            round_started=T0,
            made_at=datetime.now(UTC) + timedelta(hours=1),
            reflection=parsed,
        )

    assert "P1 says TRUE" in problem("TRUE")
    assert problem("FALSE") is None


@pytest.mark.parametrize(
    "line",
    [
        "P3 FALSE: the cap fails",
        "P3 (FALSE, 80%) — the cap fails",
        "- P3 FALSE — the cap fails",
        "**P3 FALSE** — the cap fails",
        "P2 TRUE — b",
        "P100 TRUE — c",
        # #3127 round 1 c4226814093: a bare or bodiless claim after two good ones
        "P3 FALSE",
        "P3 FALSE —",
        "P3: FALSE — x",
    ],
)
def test_a_near_miss_claim_is_refused_by_its_line(line):
    """#3120 acceptance 3, diff audit SF-2: a claim in a near-miss shape under
    the run used to end the claims silently, dropping it while the block was
    still accepted."""
    got = rr.premise_block(_block("Design-premise: SOUND", "P1 TRUE — a", "P2 TRUE — b", line))
    assert got[0] is None and "line 5 is inside the premise block" in got[2], got


@pytest.mark.parametrize(
    "text",
    [
        "note\x0bDesign-premise: BROKEN\x0bP1 FALSE — a\x0bP2 FALSE — b\n",
        "note Design-premise: BROKEN P1 FALSE — a P2 FALSE — b\n",
        "x\rDesign-premise: BROKEN\rP1 FALSE — a\rP2 FALSE — b\r",
        "Design‑premise: SOUND\nP1 TRUE — a\nP2 TRUE — b\n",
        "Design-premise: SOUND‑BUT‑INFERIOR\nP1 TRUE — a\nP2 TRUE — b\n",
        "Design-premise: SOUND\nP1 TRUE — a\nP2 TRUE — b\n",
        "﻿Design-premise: SOUND\nP1 TRUE — a\nP2 TRUE — b\n",
    ],
)
def test_lookalikes_and_in_line_separators_make_no_block(text):
    """Diff audit NOTE-1/4: lines split on newlines only, as the reflection's
    own reader does, so a block is never read out of the middle of one line;
    and a lookalike hyphen or space is not the block's grammar."""
    assert rr.premise_block(text)[0] is None


def test_a_claim_like_line_further_down_is_prose():
    """A claim-like line past the block's paragraph (after a blank line) is
    prose: a review's later prose starting with "P2" is not a claim and not
    refused."""
    text = _block() + "\nEffect: x\nP2 is the weak one\nP3 FALSE: worth a look\n"
    assert rr.premise_block(text) == ("SOUND", {"1": "TRUE", "2": "FALSE"}, None)


@pytest.mark.parametrize(
    "later",
    [
        "Design-premise: BROKEN (80%)",
        "Design-premise: BROKEN.",
        "Design-premise: BROKEN, 80%",
        "Design-premise: broken",
        "Design-premise:BROKEN",
        "**Design-premise:** BROKEN",
        "design-premise: BROKEN",
        "Design premise: BROKEN",
        "Design-premise: BROKEN \u2014",
        "Design-premise: <SOUND | SOUND-BUT-INFERIOR | BROKEN>",
    ],
)
def test_a_near_miss_verdict_line_is_refused_by_its_line(later):
    """#3127 round 1 c4226814101: a column-0 line opening like a verdict line
    but not matching it was skipped, so a contradicting closing summary passed
    the agreement check. It is refused, naming the line."""
    for text in (_block() + "\n" + later + "\n", later + "\n\n" + _block()):
        got = rr.premise_block(text)
        assert got[0] is None and "starts like a 'Design-premise:' line" in got[2], (text, got)


@pytest.mark.parametrize("quote", ["    Design-premise: BROKEN (80%)", "> Design-premise: BROKEN (80%)"])
def test_a_quoted_near_miss_verdict_line_is_prose(quote):
    """The doc's quoting rule: an indented or >-quoted verdict line is not the
    author's, so it neither counts nor refuses."""
    text = _block() + "\nAn earlier round said:\n\n" + quote + "\n"
    assert rr.premise_block(text) == ("SOUND", {"1": "TRUE", "2": "FALSE"}, None)


@pytest.mark.parametrize(
    "later",
    [
        "## Design-premise: BROKEN",
        "- Design-premise: BROKEN (80%)",
        "* Design-premise: SOUND-BUT-INFERIOR — residual",
        "1. Design-premise: BROKEN",
        "Design‑premise: BROKEN (80%)",
    ],
)
def test_a_marked_or_lookalike_verdict_is_refused(later):
    """#3127 round-1 fix audit SF-1: a verdict in a heading or list item, or
    with a lookalike hyphen, still bound silently after a valid block."""
    got = rr.premise_block(_block() + "\n" + later + "\n")
    assert got[0] is None and f"line {len(_block().split(chr(10))) + 1}" in got[2], got


def test_a_bare_section_heading_is_not_a_verdict():
    text = "## Design-premise:\n\n" + _block() + "\n### Design premise check\n"
    assert rr.premise_block(text) == ("SOUND", {"1": "TRUE", "2": "FALSE"}, None)


def test_a_restated_verdict_then_claim_like_prose_is_refused_by_line():
    """Fix audit NOTE 1: prose that starts like a claim in a restating
    block's paragraph is refused by the paragraph scan, naming the line."""
    text = _block() + "\nDesign-premise: SOUND\nP3 FALSE positives remain\n"
    got = rr.premise_block(text)
    assert got[0] is None and "line 7 is inside the premise block that starts at line 6" in got[2], got


@pytest.mark.parametrize(
    "between, culprit",
    [
        (("Expected before checking:",), 2),
        (("Expected before checking: it", "holds"), 3),
    ],
)
def test_a_broken_expected_line_is_named_not_the_claim_under_it(between, culprit):
    """Fix audit SF-2: the refusal names the line that broke the block, never
    the valid claim under it."""
    got = rr.premise_block(_block(between=between))
    assert got[0] is None and f"line {culprit} is inside the premise block" in got[2], got


@pytest.mark.parametrize(
    "line",
    [
        # #3238 item 2: every markdown claim marker, and spellings no marker list names
        "+ P3 FALSE — c",
        "# P3 FALSE — c",
        "1. P3 FALSE — c",
        "(1) P3 FALSE — c",
        "• P3 FALSE — c",
        "- [ ] P3 FALSE — c",
        "> - P3 FALSE — c",
        "P-3 FALSE — c",
        " P3 FALSE — c",
        # #3238 item 1: a misplaced Expected line, and any other prose
        "Expected before checking: later",
        "Note: one more thing",
        "Effect:",
    ],
)
def test_the_block_paragraph_is_closed(line):
    """#3238: from the verdict line to the next blank line, every line must be
    one of the block's lines. Any other line refuses by its line number rather
    than being read as prose, so no spelling of a claim can be dropped."""
    got = rr.premise_block(_block() + line + "\n")
    assert got[0] is None and "line 5 is inside the premise block" in got[2], got


def test_the_trailer_lines_and_a_closing_fence_belong_to_the_block():
    text = "```\n" + _block() + "Effect: x\nBetter shape: none found\nPR-shape: OK (10)\n```\n"
    assert rr.premise_block(text) == ("SOUND", {"1": "TRUE", "2": "FALSE"}, None)
    claim_after_trailer = _block() + "Effect: x\nP3 FALSE — c\n"
    assert "line 6 is inside the premise block" in rr.premise_block(claim_after_trailer)[2]


@pytest.mark.parametrize(
    "text",
    [
        _block(between=()),
        "Expected before checking: x\n" + _block(between=()),
        _block(between=()) + "\nExpected before checking: x\n",
    ],
)
def test_the_expected_line_is_required_directly_under_the_verdict(text):
    """#3238 item 1 (owner: Expected is required): a block without its
    Expected line, or with it anywhere but directly under the verdict, is
    refused."""
    got = rr.premise_block(text)
    assert got[0] is None and "Expected before checking" in got[2], got


def test_persisted_state_after_the_trailers_belongs_to_the_block():
    """#3238 diff audit SF-1: the architect agent's Step 0.7 emits
    Persisted-state right after PR-shape; the closed paragraph allows it."""
    text = _block() + "Effect: x\nBetter shape: none found\nPR-shape: OK (5)\nPersisted-state: NONE\n"
    assert rr.premise_block(text) == ("SOUND", {"1": "TRUE", "2": "FALSE"}, None)


def test_an_expected_line_on_a_claimless_stub_does_not_count():
    """Diff audit NOTE 2: the Expected line must sit on a block that has
    claims; a trailing verdict-plus-Expected stub does not carry it."""
    text = _block(between=()) + "\nDesign-premise: SOUND\nExpected before checking: x\n"
    got = rr.premise_block(text)
    assert got[0] is None and "Expected before checking" in got[2], got


def test_the_block_begins_at_its_verdict_line():
    """Diff audit NOTE 3, a stated residual: the paragraph is closed downward
    from the verdict line. Lines above it are prose, so the doc asks for a
    blank line above the verdict line; a claim-like line there is not read."""
    text = "P3 FALSE — c\n" + _block()
    assert rr.premise_block(text) == ("SOUND", {"1": "TRUE", "2": "FALSE"}, None)


def test_a_headline_summary_block_need_not_repeat_the_expected_line():
    text = _block(between=()) + "\n" + _block()
    assert rr.premise_block(text) == ("SOUND", {"1": "TRUE", "2": "FALSE"}, None)


@pytest.mark.parametrize(
    "line, refused",
    [
        # #3238 item 3: a heading or list item is a verdict only by its uppercase token
        ("1. Design-premise: what evidence would make this sound?", False),
        ("## Design-premise: compare sound and broken shapes", False),
        ("## Design-premise: Sound reasoning, weak evidence", False),
        ("- Design-premise: Broken", False),  # title case: stated residual, prose
        ("> Design-premise: BROKEN", False),  # the doc's quote form
        ("## Design-premise: BROKEN", True),
        ("- Design-premise: `BROKEN`", True),
        ('- Design-premise: "BROKEN"', True),
        ("- Design-premise: verdict BROKEN", True),
        ("1) Design-premise: SOUND-BUT-INFERIOR", True),
        ("| Design-premise: | BROKEN |", True),  # diff audit NOTE 4: a table row
    ],
)
def test_a_marked_verdict_line_is_judged_by_its_uppercase_token(line, refused):
    got = rr.premise_block(line + "\n\n" + _block())
    if refused:
        assert got[0] is None and "line 1 gives a 'Design-premise:' verdict" in got[2], got
    else:
        assert got == ("SOUND", {"1": "TRUE", "2": "FALSE"}, None), got


def test_a_wrapped_claim_is_refused_rather_than_dropping_the_rest():
    """#3127 round 1 class audit C8: a wrapped claim ended the run and every
    claim after it was silently dropped. #3238 closes the paragraph, so the
    wrapped tail of the LAST claim (once accepted, C9) is refused too."""
    text = _block("Design-premise: SOUND", "P1 TRUE \u2014 a", "P2 FALSE \u2014 b is long", "and wraps", "P3 FALSE \u2014 c")
    got = rr.premise_block(text)
    assert got[0] is None and "line 5 is inside the premise block" in got[2], got
    tail = _block("Design-premise: SOUND", "P1 TRUE \u2014 a", "P2 FALSE \u2014 b is long", "and wraps")
    got = rr.premise_block(tail)
    assert got[0] is None and "line 5 is inside the premise block" in got[2], got


def test_repeated_blocks_must_agree():
    """A summary may restate the verdict and any claims with the same verdicts.
    A second verdict, a changed verdict or an added claim is refused, never
    resolved."""
    two = "Design-premise: SOUND\n" + _block("Design-premise: BROKEN")
    assert "more than one" in rr.premise_block(two)[2]
    again = _block() + "\nSummary:\n\n" + _block()
    assert rr.premise_block(again)[:2] == ("SOUND", {"1": "TRUE", "2": "FALSE"})
    flipped = _block() + "\n" + _block("Design-premise: SOUND", "P1 FALSE — a", "P2 FALSE — b")
    assert "never add or change one" in rr.premise_block(flipped)[2]
    headline = "Design-premise: SOUND\n\nDetail:\n\n" + _block()
    assert rr.premise_block(headline)[:2] == ("SOUND", {"1": "TRUE", "2": "FALSE"})
    partial = _block() + "\nSummary:\nDesign-premise: SOUND\nP2 FALSE — b\n"
    assert rr.premise_block(partial)[:2] == ("SOUND", {"1": "TRUE", "2": "FALSE"})


@pytest.mark.parametrize(
    "later",
    [
        # diff audit SF-1(a): a restated verdict line, then a claim the first block lacks
        "\nDesign-premise: SOUND\nP3 FALSE \u2014 positives remain in the corpus\n",
        # SF-1(b): a column-0 quote of an earlier round's block inside a fence
        "\n```\nDesign-premise: SOUND\nP3 TRUE — old claim\nP4 FALSE — old\n```\n",
    ],
)
def test_a_later_block_cannot_add_claims(later):
    """A later block may only restate the first one; claims the author never
    stated in their own block are never read as theirs."""
    got = rr.premise_block(_block() + later)
    assert got[0] is None and "never add or change one" in got[2], got


def _bound(tmp_path, *, premise="SOUND-BUT-INFERIOR", cited="SOUND-BUT-INFERIOR", checks=CHECK_LINES):
    lines = [*checks, f"Premise-evidence: {_strong_premise(tmp_path)} {cited}"]
    parsed = rr.parse(_reflection(premise=premise, extra=lines), round_number=2)
    assert parsed.ok, parsed.problems
    return rr.evidence_problem(
        parsed.premise_evidence,
        kind="premise",
        cwd=str(tmp_path),
        round_started=T0,
        made_at=datetime.now(UTC) + timedelta(hours=1),
        reflection=parsed,
    )


def test_the_premise_verdict_is_bound_to_the_reflection(tmp_path):
    """#3107 c4222860280: the file's verdict was checked only for existence, so
    a file concluding BROKEN could be cited by a reflection saying SOUND."""
    assert _bound(tmp_path) is None
    assert "cites it as SOUND" in _bound(tmp_path, cited="SOUND")
    assert "Premise is SOUND" in _bound(tmp_path, premise="SOUND")
    flipped = ["Premise-check: P1 FALSE the store already dedups", CHECK_LINES[1]]
    assert "P1 says FALSE" in _bound(tmp_path, checks=flipped)
    unknown = [*CHECK_LINES, "Premise-check: P7 TRUE a claim the file never made"]
    assert "has no claim P7" in _bound(tmp_path, checks=unknown)


def test_a_premise_evidence_line_needs_its_verdict():
    line = "Premise-evidence: ~/.genesis/review_evidence/p.txt"
    got = rr.parse(_reflection(extra=[*CHECK_LINES, line]), round_number=2)
    assert any("not a recognised field" in p for p in got.problems)


def test_a_reflection_giving_one_claim_two_verdicts_is_refused():
    twice = [*CHECK_LINES, "Premise-check: P1 FALSE the store already dedups by key"]
    got = rr.parse(_reflection(extra=twice), round_number=1)
    assert any("given two verdicts" in p for p in got.problems)


def test_a_broken_premise_check_cannot_cover_its_findings(repo, tmp_path):
    """The file decides: BROKEN cited honestly escalates, and cited as anything
    else it fails the binding; either way nothing is covered."""
    path, first = repo
    _commit_reflection(path, tmp_path, _reflection(head=first))
    _fix(path, "fix\n")
    second = _git(path, "rev-parse", "HEAD").strip()
    broken = tmp_path / "broken.txt"
    broken.write_text(PREMISE_BLOCK.replace("SOUND-BUT-INFERIOR", "BROKEN"))
    for premise in ("BROKEN", "SOUND-BUT-INFERIOR"):
        lines = [*CHECK_LINES, f"Premise-evidence: {broken} {premise}"]
        _commit_reflection(path, tmp_path, _reflection(head=second, premise=premise, extra=lines))
    assert _covered(path, second, round_number=2, prior_heads=[first]) == set()


def test_a_round_two_reflection_needs_a_real_premise_check(repo, tmp_path):
    path, first = repo
    _commit_reflection(path, tmp_path, _reflection(head=first))
    _fix(path, "fix\n")
    second = _git(path, "rev-parse", "HEAD").strip()
    summary = tmp_path / "summary.txt"
    summary.write_text("Premise: SOUND\n| 1 | TRUE |\n| 2 | FALSE |\n")
    lines = [*CHECK_LINES, f"Premise-evidence: {summary} SOUND"]
    sound = "SOUND-BUT-INFERIOR"
    _commit_reflection(path, tmp_path, _reflection(head=second, premise=sound, extra=lines))
    assert _covered(path, second, round_number=2, prior_heads=[first]) == set()
    owed = _round_two(tmp_path)
    _commit_reflection(path, tmp_path, _reflection(head=second, premise=sound, extra=owed))
    assert _covered(path, second, round_number=2, prior_heads=[first]) == {"c1", "c2"}


def test_an_audit_written_after_the_reflection_is_refused(repo, tmp_path):
    """Round 2 of #3055: the evidence was bound by path alone, so citing a
    missing audit, fixing, then writing the audit counted."""
    path, head = repo
    evidence = tmp_path / "late-audit.txt"
    cited = _reflection(head=head, extra=[f"Audit-evidence: {evidence} PASS"])
    _commit_reflection(path, tmp_path, cited)
    when = _git(path, "log", "-1", "--format=%ct").strip()
    evidence.write_text(_strong_audit(tmp_path).read_text())
    later = int(when) + 60
    os.utime(evidence, (later, later))
    assert _covered(path, head, gate_lane=True) == set()
    os.utime(evidence, (int(when) - 5, int(when) - 5))
    assert _covered(path, head, gate_lane=True) == {"c1", "c2"}


def test_a_legacy_encoded_commit_message_is_read_not_a_crash(repo, tmp_path):
    """Round 1 of #3055: git emits raw bytes for a commit made under a legacy
    i18n.commitEncoding; reading the log must not raise."""
    path, head = repo
    _git(path, "config", "i18n.commitEncoding", "latin1")
    message = tmp_path / "latin.txt"
    message.write_bytes("Round-reflection: caf\xe9\n".encode("latin1"))
    _git(path, "commit", "-q", "--allow-empty", "-F", str(message))
    assert _covered(path, head) == set()


def test_acceptance_points_are_checked_when_given(repo, tmp_path):
    path, head = repo
    _commit_reflection(path, tmp_path, _reflection(head=head))
    assert _covered(path, head, acceptance=["the key round-trips"]) == {"c1", "c2"}
    assert _covered(path, head, acceptance=["an unmapped point"]) == set()


@pytest.mark.parametrize(
    "scopes, ok",
    [
        (("covered: the gate refuses an unreadable PR body",), True),
        (("covered: the gate refuses", "covered: an unreadable PR body"), False),
        (("not covered: the gate refuses an unreadable PR body",), False),
        (("deferred: the gate refuses an unreadable PR body",), False),
    ],
)
def test_an_acceptance_point_maps_within_one_covered_scope_line(scopes, ok):
    """Round 2 of #3055: a point split across Scope lines, or named on a
    line that says it is not covered, was accepted."""
    point = "the gate refuses an unreadable PR body"
    got = rr.parse(_reflection(scopes=scopes), round_number=1, acceptance=[point])
    assert got.ok is ok, got.problems


def test_an_open_round_with_unknown_keys_still_reports_its_timing():
    """Secondary review P3: owed stays unknown, but the settle window is still
    reported, so a reader can tell when the round settles."""
    got = rr.owed_state(_budget(reflection_keys="unknown"), (), now=T0 + timedelta(hours=1))
    assert got["owed"] is None
    assert got["round_started"] == T0.isoformat() and got["settled"]
    assert got["settle_until"] == (T0 + rr.SETTLE).isoformat()


def _status_with(monkeypatch, path, budget):
    import review_budget

    monkeypatch.setattr(rr, "pr_identity", lambda cwd: ("o/r", 7))
    monkeypatch.setattr(review_budget, "evaluate_pr", lambda repo, number: budget)

    def no_second_read(repo, number):
        raise AssertionError("the body came with the budget; no gh pr view")

    monkeypatch.setattr(rr, "_pr_meta", no_second_read)
    return rr.status(str(path), now=T0 + timedelta(hours=1))


def test_status_binds_acceptance_to_the_budget_body(repo, tmp_path, monkeypatch):
    """#3107 c4222860305: the acceptance points come from the body the budget
    read, and no second read of the PR is made."""
    path, head = repo
    _commit_reflection(path, tmp_path, _reflection(keys=("c1",), head=head))
    plain = _budget(open_keys=["c1"], current_head=head, body="no acceptance section")
    assert _status_with(monkeypatch, path, plain)["owed"] == []
    # Control: the same reflection under a body that declares an acceptance
    # point it never maps is not covered, so the body really was read.
    declared = dict(plain, body="## Acceptance\n- the cap holds on every path\n")
    assert _status_with(monkeypatch, path, declared)["owed"] == ["c1"]


def test_status_refuses_a_body_that_changed_between_reads(repo, monkeypatch):
    path, head = repo
    budget = _budget(current_head=head, body=None, body_changed=True)
    with pytest.raises(rr.Refused, match="changed between the two reads"):
        _status_with(monkeypatch, path, budget)


def test_status_refuses_a_carried_body_it_cannot_read(repo, monkeypatch):
    """A budget that carries the key but no text never falls back to a third,
    unsynchronised read."""
    path, head = repo
    with pytest.raises(rr.Refused, match="could not be read"):
        _status_with(monkeypatch, path, _budget(current_head=head, body=None))


def test_status_subtracts_what_is_covered(repo, tmp_path, monkeypatch):
    path, head = repo
    _commit_reflection(path, tmp_path, _reflection(keys=("c1",), head=head))
    import review_budget

    budget = _budget(open_keys=["c1", "c2"], current_head=head)
    monkeypatch.setattr(rr, "pr_identity", lambda cwd: ("o/r", 7))
    monkeypatch.setattr(rr, "_pr_meta", lambda repo, number: {"body": "", "baseRefName": "main"})
    monkeypatch.setattr(review_budget, "evaluate_pr", lambda repo, number: budget)
    got = rr.status(str(path), now=T0 + timedelta(hours=1))
    assert got["owed"] == ["c2"] and got["settled"]


def test_status_judges_each_open_key_by_its_review_in_the_budget(repo, tmp_path, monkeypatch):
    """The budget's own review times reach the credit rule: a commit made
    after the head and before the reviews is no fix, after them it is."""
    path, head = repo
    _change(path, "local\n", _at(-10))
    _commit_reflection(path, tmp_path, _reflection(head=head))
    reviews = [
        {"submitted_at": _at(0).isoformat(), "finding_keys": ["c1"]},
        {"submitted_at": _at(-20).isoformat(), "finding_keys": ["c2"]},
    ]
    budget = _budget(
        open_keys=["c1", "c2"],
        current_head=head,
        body="",
        rounds=[{"head": head, "reviews": reviews}],
    )
    assert _status_with(monkeypatch, path, budget)["owed"] == ["c2"]


@pytest.mark.parametrize(
    "first, second, want",
    [
        (_at(0), _at(30), _at(0)),
        (_at(30), _at(0), _at(0)),
        (None, _at(0), None),
        (_at(0), None, None),
    ],
)
def test_a_key_listed_twice_takes_its_earliest_or_unknown_time(first, second, want):
    def review(when):
        return {"submitted_at": when.isoformat() if when else None, "finding_keys": ["c1"]}

    budget = _budget(rounds=[{"head": HEAD, "reviews": [review(first), review(second)]}])
    assert rr._open_review_times(budget) == {"c1": want}


def test_status_is_owed_from_budget_plus_who_and_what_went_wrong(repo, tmp_path, monkeypatch):
    """One answer for status and the commit gate: status adds only the PR and
    the budget's errors to what owed_from_budget says."""
    path, head = repo
    _commit_reflection(path, tmp_path, _reflection(keys=("c1",), head=head))
    budget = _budget(open_keys=["c1", "c2"], current_head=head, body="", errors=["e"])
    got = _status_with(monkeypatch, path, budget)
    shared = rr.owed_from_budget(
        str(path), budget, "o/r", 7, now=T0 + timedelta(hours=1), cutover=None
    )
    assert got == {**shared, "repo": "o/r", "pr": 7, "errors": ["e"]}
    assert got["owed"] == ["c2"]


def test_owed_from_budget_refuses_past_its_deadline(repo, tmp_path):
    path, head = repo
    _commit_reflection(path, tmp_path, _reflection(keys=("c1",), head=head))
    budget = _budget(open_keys=["c1"], current_head=head, body="")
    with pytest.raises(rr.Refused, match="ran out of time"):
        rr.owed_from_budget(str(path), budget, "o/r", 7, now=T0, cutover=None, deadline=0.0)


def test_unreadable_git_is_refused_never_nothing_owed(tmp_path):
    with pytest.raises(rr.Refused):
        rr.covered_keys(
            str(tmp_path),
            HEAD,
            round_number=1,
            gate_lane=False,
            prior_heads=[],
            acceptance=None,
            round_started=T0,
        )


# -- the CLI -----------------------------------------------------------------


def test_validate_exit_codes(tmp_path):
    good = tmp_path / "good.md"
    good.write_text(_reflection())
    bad = tmp_path / "bad.md"
    bad.write_text("not a reflection\n")
    binary = tmp_path / "binary.md"
    binary.write_bytes(b"\xff\xfe")
    assert rr.main(["validate", str(good), "--round", "1"]) == 0
    assert rr.main(["validate", str(bad)]) == 1
    assert rr.main(["validate", str(tmp_path / "absent.md")]) == 2
    assert rr.main(["validate", str(binary)]) == 2


def test_an_unexpected_error_in_status_is_cannot_check_not_invalid(monkeypatch):
    """Fix-code audit N5: exit 1 means invalid; status never returns it."""

    def boom(cwd, **kw):
        raise AttributeError("a malformed rounds entry")

    monkeypatch.setattr(rr, "status", boom)
    assert rr.main(["status"]) == 2


# -- the open round is the head's own entry, never rounds[-1] (#3302) --------

OLD = "b" * 40


def _entry(head: str, minutes: int, key: str) -> dict:
    review = {"submitted_at": _at(minutes).isoformat(), "finding_keys": [key]}
    return {"head": head, "reviews": [review]}


def _head_listed_first(**over):
    """An open round on HEAD whose entry is listed BEFORE an older round's, as a
    commit list ordered by date puts it after a rebase or cherry-pick."""
    return _budget(
        count=2,
        open_keys=["c2"],
        rounds=[_entry(HEAD, 5, "c2"), _entry(OLD, 0, "c1")],
        **over,
    )


def test_the_open_round_is_read_by_the_heads_identity():
    got = rr.owed_state(_head_listed_first(), [], now=_at(60))
    assert got["owed"] == ["c2"] and got["round_head"] == HEAD
    assert got["round_started"] == _at(5).isoformat()
    assert rr._open_review_times(_head_listed_first()) == {"c2": _at(5)}


def test_prior_heads_are_every_round_but_the_heads_own(monkeypatch):
    seen = {}

    def covered(cwd, head, **kw):
        seen.update(kw, head=head)
        return set()

    monkeypatch.setattr(rr, "covered_keys", covered)
    budget = _head_listed_first()
    rr._open_covered("/x", budget, rr.owed_state(budget, [], now=_at(60)), None)
    assert seen["head"] == HEAD and seen["prior_heads"] == [OLD]
    assert seen["reviewed_at"] == {"c2": _at(5)}


@pytest.mark.parametrize(
    "rounds",
    [
        [{"head": OLD, "reviews": []}],  # the producer says open, no entry for the head
        [{"head": HEAD, "reviews": []}, {"head": HEAD, "reviews": []}],  # two entries
    ],
)
def test_an_open_round_without_one_entry_for_the_head_owes_unknown(rounds):
    got = rr.owed_state(_budget(rounds=rounds), [], now=_at(60))
    assert got["owed"] is None and got["round_started"] is None


def test_owed_from_budget_keeps_an_inconsistent_open_round_unknown(repo):
    """Coverage narrows a KNOWN debt only; it never turns None into a list."""
    path, head = repo
    budget = _budget(current_head=head, body="", rounds=[_entry(OLD, 0, "c1")])
    got = rr.owed_from_budget(str(path), budget, "o/r", 7, now=T0, cutover=None)
    assert got["owed"] is None


def test_a_complete_round_names_no_round_head():
    """``round_head`` was the newest entry by list order, a guess; with no round
    open there is no head-owned round to name."""
    got = rr.owed_state(_budget(round_state="complete"), [], now=T0)
    assert got["round_head"] is None and got["owed"] == []
