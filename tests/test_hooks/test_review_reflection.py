"""The round-reflection tool: format, what is owed, coverage, and the commit.

Network calls are replaced: ``status`` by a fixed state, and the PR metadata,
privacy scan and comment listing by stubs, except where the real privacy
scanner's behaviour is the point. Commits run in a real temporary git
repository, because cleanup modes, hooks and the empty-tree check are git
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
    verdict="LEAD the change routes around an existing store",
    decision="close-class the same mistake appears twice",
    classes=("unchecked subprocess result: 2",),
    dispositions=None,
    scope=("- covered: the key round-trips (tests/test_x.py)",),
    extra="",
):
    if dispositions is None:
        dispositions = [f"- {k}: fix-now (test 1)" for k in keys]
    return "\n".join(
        [
            f"Round-reflection: keys={','.join(keys)} head={head}",
            "",
            "## Distribution",
            *[f"- {c}" for c in classes],
            "Concentration: scripts/x.py x2",
            "Fix-induced: 0 of 2 blamed findings sit on lines this branch wrote",
            "Trend: first round",
            "",
            "## Premise",
            f"Design-premise: {verdict}",
            f"{rr.IMPOSSIBLE_PROMPT} route every call through one checked helper",
            "",
            "## Scope",
            *scope,
            "",
            "## Decision",
            f"Decision: {decision}",
            "",
            "## Dispositions",
            *dispositions,
            extra,
            "Padding so the reflection clears the minimum length of a real one. " * 4,
        ]
    )


# -- obligations and parsing -------------------------------------------------


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
    assert got.decision == "close-class" and not got.escalate


def test_body_and_comment_keys_are_in_the_grammar():
    text = _reflection(keys=("c1", "r20:2", "i30"))
    assert rr.parse(text, round_number=1).ok
    assert not rr.parse(_reflection(keys=("r20/1",))).ok


@pytest.mark.parametrize(
    "text, fragment",
    [
        ("Round-reflection: head=" + HEAD, "FIRST line"),
        ("\n" + _reflection(), "FIRST line"),
        ("  " + _reflection(), "FIRST line"),
        (_reflection() + f"\nRound-reflection: keys=c9 head={HEAD}\n", "exactly one header"),
        (_reflection(keys=("c1", "c1")), "names a key twice"),
        (_reflection(dispositions=["- c1: fix-now (test 1)"]), "no disposition for: c2"),
        (
            _reflection(
                dispositions=[
                    "- c1: fix-now (test 1)",
                    "- c2: file (fails all four; #9)",
                    "- c3: fix-now (test 2)",
                ]
            ),
            "header does not name: c3",
        ),
        (
            _reflection(dispositions=["- c1: fix-now (test 5)", "- c2: fix-now (test 1)"]),
            "c1: a disposition is",
        ),
        (
            _reflection(
                dispositions=[
                    "- c1: fix-now (test 1)",
                    "- c1: fix-now (test 2)",
                    "- c2: fix-now (test 1)",
                ]
            ),
            "c1 has two dispositions",
        ),
        (
            _reflection(dispositions=["- c1: file (later)", "- c2: fix-now (test 1)"]),
            "c1: a disposition is",
        ),
        (_reflection(extra=f"{rr.FILL} later>"), "unfilled template placeholder"),
    ],
)
def test_coverage_problems_are_named(text, fragment):
    got = rr.parse(text)
    assert not got.ok and any(fragment in p for p in got.problems), got.problems


def test_a_short_reflection_is_refused():
    text = "Round-reflection: keys=c1 head=" + HEAD + "\n## Dispositions\n- c1: fix-now (test 1)\n"
    assert any("at least" in p for p in rr.parse(text).problems)


def test_a_disposition_hidden_in_a_fence_or_another_section_does_not_count():
    fenced = _reflection(
        dispositions=[
            "- c1: fix-now (test 1)",
            "```",
            "## Dispositions",
            "- c2: fix-now (test 1)",
            "```",
        ]
    )
    assert any("no disposition for: c2" in p for p in rr.parse(fenced).problems)
    elsewhere = _reflection(dispositions=["- c1: fix-now (test 1)"]).replace(
        "## Scope\n", "## Scope\n- c2: fix-now (test 1)\n"
    )
    assert any("no disposition for: c2" in p for p in rr.parse(elsewhere).problems)


@pytest.mark.parametrize(
    "kwargs, fragment",
    [
        ({"classes": ("no count here",)}, "Distribution needs"),
        ({"verdict": "LEAD"}, "Design-premise"),
        ({"decision": "fix-instances"}, "Decision needs"),
        ({"scope": ("no bullet",)}, "Scope needs"),
    ],
)
def test_round_parts_are_required(kwargs, fragment):
    got = rr.parse(_reflection(**kwargs), round_number=1)
    assert any(fragment in p for p in got.problems), got.problems


def test_an_early_verdict_cannot_hide_a_later_one():
    text = _reflection(verdict="SOUND fine\nDesign-premise: BROKEN wrong store")
    got = rr.parse(text, round_number=2)
    assert any("more than once" in p for p in got.problems)


def test_a_decision_outside_its_section_is_refused():
    text = _reflection(scope=("- x", "Decision: close-class decoy"))
    assert any("only in its own section" in p for p in rr.parse(text, round_number=1).problems)


def test_the_impossible_question_must_be_answered_in_premise():
    blank = _reflection().replace(" route every call through one checked helper", "")
    assert any("whole class impossible" in p for p in rr.parse(blank, round_number=1).problems)
    nextline = _reflection().replace(" route every call", "\nroute every call")
    assert rr.parse(nextline, round_number=1).ok


def test_lead_is_only_a_round_one_verdict():
    got = rr.parse(_reflection(), round_number=2)
    assert any("from round 2" in p for p in got.problems)


def test_round_obligations_by_lane():
    audit = "Audit-evidence: ~/.genesis/review_evidence/x.txt NO BLOCKER"
    premise = "\n## Premise-check\n- P1 the store dedups TRUE\n- P2 replies are top-level FALSE\n"
    sound = "SOUND-BUT-INFERIOR a shared helper would remove the class"
    assert any(
        "fresh-context audit" in p
        for p in rr.parse(_reflection(), round_number=1, gate_lane=True).problems
    )
    assert rr.parse(_reflection(extra=audit), round_number=1, gate_lane=True).ok
    two = rr.parse(_reflection(verdict=sound), round_number=2, gate_lane=True)
    assert any("premise check" in p for p in two.problems)
    assert rr.parse(_reflection(verdict=sound, extra=premise), round_number=2, gate_lane=True).ok
    assert rr.parse(_reflection(verdict=sound, extra=audit), round_number=2).ok
    one_check = "\n## Premise-check\n- P1 holds TRUE\n"
    assert not rr.parse(_reflection(verdict=sound, extra=one_check), round_number=3).ok


def test_a_recurring_class_forbids_fixing_instances():
    text = _reflection(decision="fix-instances patch both")
    got = rr.parse(text, round_number=1, previous_classes=["Unchecked  subprocess result"])
    assert any("recurs" in p for p in got.problems)
    assert rr.parse(
        _reflection(), round_number=1, previous_classes=["unchecked subprocess result"]
    ).ok


def test_each_acceptance_point_must_appear_in_scope():
    point = "Every finding is keyed by an immutable id"
    missing = rr.parse(_reflection(), round_number=1, acceptance=[point])
    assert any("acceptance point" in p for p in missing.problems)
    mapped = _reflection(scope=(f"- covered: {point.lower()} (tests/test_x.py)",))
    assert rr.parse(mapped, round_number=1, acceptance=[point]).ok


@pytest.mark.parametrize(
    "text",
    [
        _reflection(verdict="BROKEN the store already exists"),
        _reflection(verdict="SUSPECT the premise may be wrong"),
        _reflection(extra="escalate: YES"),
        "Escalate: yes\n" + _reflection(),
    ],
)
def test_escalation_is_flagged(text):
    assert rr.parse(text).escalate


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
                "reviews": [
                    {"submitted_at": T0.isoformat()},
                    {"submitted_at": (T0 + timedelta(minutes=5)).isoformat()},
                ]
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
    assert got["round_started"] == T0.isoformat()


def test_it_settles_early_once_every_expected_reviewer_has_reported():
    early = T0 + timedelta(minutes=1)
    waiting = _budget(expected_reviewers=["a[bot]", "b[bot]"], reviewers_reported=["a[bot]"])
    assert not rr.owed_state(waiting, [], now=early)["settled"]
    assert rr.owed_state(dict(waiting, reviewers_reported=["a[bot]", "b[bot]"]), [], now=early)[
        "settled"
    ]


def test_round_one_with_nobody_expected_waits_the_whole_window():
    assert not rr.owed_state(_budget(expected_reviewers=[]), [], now=T0 + timedelta(minutes=1))[
        "settled"
    ]


def test_no_readable_review_time_never_settles():
    got = rr.owed_state(_budget(rounds=[{"reviews": [{"submitted_at": None}]}]), [], now=T0)
    assert not got["settled"] and got["settle_until"] is None


@pytest.mark.parametrize(
    "where",
    [
        "   Design-premise: BROKEN indented",
        "```\nDesign-premise: SUSPECT inside a fence\n```",
        "> Escalate: yes",
    ],
)
def test_escalation_is_read_from_every_line(where):
    """Re-audit S2: a flag placed before the sections, indented or fenced must
    still escalate, never be lost to its placement."""
    assert rr.parse(_reflection(extra=where)).escalate


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
    args = ["commit", "-q", "--cleanup=verbatim", "-F", str(message)]
    _git(repo, *(args[:1] + (["--allow-empty"] if empty else []) + args[1:]))
    return _git(repo, "rev-parse", "HEAD").strip()


@pytest.fixture
def repo(tmp_path):
    """A base branch with one commit, and a PR branch cut from it."""
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.name", "t")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "commit.cleanup", "strip")  # would strip '## ' headings
    (path / "f.txt").write_text("x\n")
    _git(path, "add", "f.txt")
    _git(path, "commit", "-q", "-m", "base")
    _git(path, "checkout", "-q", "-b", "feat/x")
    (path / "f.txt").write_text("y\n")
    _git(path, "commit", "-q", "-am", "the change")
    return path, _git(path, "rev-parse", "HEAD").strip()


def _covered(path, head, **kw):
    kw.setdefault("round_number", 1)
    kw.setdefault("gate_lane", False)
    return rr.covered_keys(str(path), head, base_ref="main", **kw)


def test_an_empty_reflection_on_the_head_covers_its_keys(repo, tmp_path):
    path, head = repo
    _commit_reflection(path, tmp_path, _reflection(head=head))
    assert _covered(path, head) == {"c1", "c2"}


def test_appended_trailers_keep_the_reflection_counted(repo, tmp_path):
    """This install's prepare-commit-msg appends Install: and Genesis-Session:."""
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
        (lambda h: _reflection(head=h, verdict="BROKEN wrong store"), "it escalates"),
        (lambda h: _reflection(head=h, decision=""), "no decision (round-aware parse)"),
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


def test_a_hand_made_reflection_meets_the_recurring_class_rule(repo, tmp_path):
    """Re-audit S1: the rule commit enforced must hold for a hand-made one too."""
    path, first = repo
    _commit_reflection(path, tmp_path, _reflection(head=first))
    (path / "f.txt").write_text("fix\n")
    _git(path, "commit", "-q", "-am", "fix")
    second = _git(path, "rev-parse", "HEAD").strip()
    sound = "SOUND-BUT-INFERIOR one helper would remove it"
    evidence = tmp_path / "audit.txt"
    evidence.write_text(
        "Severity ladder: BLOCKER none; SHOULD-FIX scripts/x.py:12 the loop is unbounded; "
        "NOTE scripts/y.py:3 a stale comment. Scope: the whole diff was read. " * 4
    )
    audit = f"Audit-evidence: {evidence} PASS"
    repeat = _reflection(head=second, verdict=sound, decision="fix-instances again", extra=audit)
    _commit_reflection(path, tmp_path, repeat)
    assert _covered(path, second, round_number=2) == set()
    # The control: the same reflection closing the class is covered, so only the
    # recurring-class rule refused the first one.
    closing = _reflection(
        head=second, verdict=sound, decision="close-class one helper", extra=audit
    )
    _commit_reflection(path, tmp_path, closing)
    assert _covered(path, second, round_number=2) == {"c1", "c2"}


def test_cited_audit_evidence_is_checked(repo, tmp_path):
    path, head = repo
    missing = _reflection(head=head, extra="Audit-evidence: ~/.genesis/no-such-audit.txt PASS")
    _commit_reflection(path, tmp_path, missing)
    assert _covered(path, head, gate_lane=True) == set()
    strong = tmp_path / "audit.txt"
    strong.write_text(
        "Severity ladder: BLOCKER none; SHOULD-FIX scripts/x.py:12 the loop is unbounded; "
        "NOTE scripts/y.py:3 a stale comment. Scope: the whole diff was read. " * 4
    )
    _commit_reflection(
        path, tmp_path, _reflection(head=head, extra=f"Audit-evidence: {strong} PASS")
    )
    assert _covered(path, head, gate_lane=True) == {"c1", "c2"}
    later = datetime.now(UTC) + timedelta(hours=1)
    assert _covered(path, head, gate_lane=True, round_started=later) == set()


def test_acceptance_points_are_checked_when_given(repo, tmp_path):
    path, head = repo
    _commit_reflection(path, tmp_path, _reflection(head=head))
    assert _covered(path, head, acceptance=["the key round-trips"]) == {"c1", "c2"}
    assert _covered(path, head, acceptance=["an unmapped point"]) == set()


def test_previous_classes_come_from_this_branch_and_an_earlier_head(repo, tmp_path):
    """Re-audit S3: a reflection on the base branch (a stacked PR's base) is
    not this branch's previous round; one on the same head is not either."""
    path, head = repo
    _git(path, "checkout", "-q", "main")
    _commit_reflection(path, tmp_path, _reflection(head="c" * 40, classes=("base class: 1",)))
    _git(path, "checkout", "-q", "feat/x")
    _git(path, "rebase", "-q", "main")
    head = _git(path, "rev-parse", "HEAD").strip()
    assert rr.previous_class_labels(str(path), head, base_ref="main") == []
    _commit_reflection(path, tmp_path, _reflection(head="b" * 40, classes=("old class: 1",)))
    _commit_reflection(path, tmp_path, _reflection(head=head, classes=("this round: 1",)))
    assert rr.previous_class_labels(str(path), head, base_ref="main") == ["old class"]


def test_status_subtracts_what_is_covered(repo, tmp_path, monkeypatch):
    """Re-audit S5: the path from covered_keys to what is owed, end to end."""
    path, head = repo
    _commit_reflection(path, tmp_path, _reflection(keys=("c1",), head=head))
    import review_budget

    budget = _budget(open_keys=["c1", "c2"], current_head=head)
    monkeypatch.setattr(rr, "pr_identity", lambda cwd: ("o/r", 7))
    monkeypatch.setattr(rr, "_pr_meta", lambda repo, number: {"body": "", "baseRefName": "main"})
    monkeypatch.setattr(review_budget, "evaluate_pr", lambda repo, number: budget)
    monkeypatch.setattr(rr, "covered_keys", _with_base("main", rr.covered_keys))
    got = rr.status(str(path), now=T0 + timedelta(hours=1))
    assert got["owed"] == ["c2"] and got["settled"]


def _with_base(base, real):
    def call(cwd, head, **kw):
        kw["base_ref"] = base
        return real(cwd, head, **kw)

    return call


def test_unreadable_git_is_refused_never_nothing_owed(tmp_path):
    with pytest.raises(rr.Refused):
        rr.covered_keys(str(tmp_path), HEAD, round_number=1, gate_lane=False, base_ref="main")


def test_a_reflection_made_after_a_fix_covers_nothing(repo, tmp_path):
    """Secondary review P2: reflect-then-fix and fix-then-reflect must differ."""
    path, head = repo
    (path / "f.txt").write_text("fix\n")
    _git(path, "commit", "-q", "-am", "fix")
    _commit_reflection(path, tmp_path, _reflection(head=head))
    assert _covered(path, head) == set()


def test_a_second_reflection_for_the_same_round_still_counts(repo, tmp_path):
    path, head = repo
    _commit_reflection(path, tmp_path, _reflection(keys=("c1",), head=head))
    _commit_reflection(path, tmp_path, _reflection(keys=("c9",), head=head))
    assert _covered(path, head) == {"c1", "c9"}


def test_validate_reports_a_missing_file_as_unable_not_invalid(tmp_path, capsys):
    assert rr.main(["validate", str(tmp_path / "absent.md")]) == 2
