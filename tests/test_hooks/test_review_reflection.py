"""The round reflection: the closed header-block grammar and its coverage check.

The format is a closed block of fixed ``Field: value`` lines (owner ruling
2026-10-07), so most tests are tables: every markdown construct the earlier
regex parser had to special-case is now simply refused inside the block and
ignored in the prose after it. Coverage tests run in real temporary git
repositories, because emptiness, ancestry, hooks and encodings are git
behaviours.
"""

from __future__ import annotations

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
        (2, False, (True, False)),
        (3, False, (False, True)),
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


def test_round_obligations_by_lane():
    audit = ["Audit-evidence: ~/.genesis/review_evidence/x.txt NO BLOCKER"]
    checks = [
        "Premise-check: P1 TRUE the store already dedups by key",
        "Premise-check: P2 FALSE replies are never top level",
    ]
    sound = "SOUND-BUT-INFERIOR"
    assert any(
        "fresh-context audit" in p
        for p in rr.parse(_reflection(), round_number=1, gate_lane=True).problems
    )
    assert rr.parse(_reflection(extra=audit), round_number=1, gate_lane=True).ok
    two = rr.parse(_reflection(premise=sound), round_number=2, gate_lane=True)
    assert any("premise check" in p for p in two.problems)
    assert rr.parse(_reflection(premise=sound, extra=checks), round_number=2, gate_lane=True).ok
    twice = [checks[0], checks[0]]
    again = rr.parse(_reflection(premise=sound, extra=twice), round_number=2, gate_lane=True)
    assert any("premise check" in p for p in again.problems), "one check written twice"
    assert rr.parse(_reflection(premise=sound, extra=audit), round_number=2).ok
    assert not rr.parse(_reflection(premise=sound, extra=checks[:1]), round_number=3).ok


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


def test_a_merge_between_the_head_and_the_reflection_disqualifies_it(repo, tmp_path):
    path, head = repo
    _git(path, "checkout", "-q", "main")
    (path / "g.txt").write_text("main\n")
    _git(path, "add", "g.txt")
    _git(path, "commit", "-q", "-m", "main moves")
    _git(path, "checkout", "-q", "feat/x")
    _git(path, "merge", "-q", "--no-edit", "main")
    _commit_reflection(path, tmp_path, _reflection(head=head))
    assert _covered(path, head) == set()


def test_a_merge_that_changes_no_tree_still_disqualifies_it(repo, tmp_path):
    """Only the one-parent rule decides this: `-s ours` keeps the tree."""
    path, head = repo
    _git(path, "checkout", "-q", "-b", "side", "main")
    _git(path, "commit", "-q", "--allow-empty", "-m", "side")
    _git(path, "checkout", "-q", "feat/x")
    _git(path, "merge", "-q", "--no-edit", "-s", "ours", "side")
    assert _git(path, "rev-parse", "HEAD^{tree}") == _git(path, "rev-parse", "HEAD^1^{tree}")
    _commit_reflection(path, tmp_path, _reflection(head=head))
    assert _covered(path, head) == set()


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
    audit = [f"Audit-evidence: {_strong_audit(tmp_path)} PASS"]
    sound = "SOUND-BUT-INFERIOR"
    repeat = _reflection(head=second, premise=sound, decision="fix-instances", extra=audit)
    _commit_reflection(path, tmp_path, repeat)
    assert _covered(path, second, round_number=2, prior_heads=[first]) == set()
    closing = _reflection(head=second, premise=sound, decision="close-class", extra=audit)
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
    audit = [f"Audit-evidence: {_strong_audit(tmp_path)} PASS"]
    sound = "SOUND-BUT-INFERIOR"
    repeat = _reflection(head=second, premise=sound, decision="fix-instances", extra=audit)
    _commit_reflection(path, tmp_path, repeat)
    assert _covered(path, second, round_number=2, prior_heads=[first]) == set()


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
    assert "not valid UTF-8" in rr.audit_problem(str(bad), T0)


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
