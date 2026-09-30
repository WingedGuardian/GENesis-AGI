"""The reviewer list (`review_findings.REVIEWERS`, reviewed code) and its readers."""

from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "scripts"))

import review_findings as rf  # noqa: E402

CODEX = "chatgpt-codex-connector[bot]"
DEVIN = "devin-ai-integration[bot]"
RABBIT = "coderabbitai[bot]"
CODEQL = "github-advanced-security[bot]"
H = "a" * 40


def test_the_shipped_list_is_the_installed_review_apps_in_order():
    """Codex primary, Devin and CodeRabbit able to stand in, CodeQL shown but never
    counted."""
    assert (
        rf.Reviewer(login=CODEX, parser="codex-badge", primary=True),
        rf.Reviewer(login=DEVIN, parser="devin-marker"),
        rf.Reviewer(login=RABBIT, parser="coderabbit-header"),
        rf.Reviewer(login=CODEQL, parser="codeql", surface_only=True),
    ) == rf.REVIEWERS
    assert {"codex-badge", "coderabbit-header", "devin-marker", "codeql"} == rf.PARSERS
    assert [f.name for f in dataclasses.fields(rf.Reviewer)] == [
        "login",
        "parser",
        "primary",
        "surface_only",
    ]
    rf._validate(rf.REVIEWERS)  # and it passes its own invariants


def test_reviewers_are_immutable():
    with pytest.raises(dataclasses.FrozenInstanceError):
        rf.REVIEWERS[0].primary = False  # type: ignore[misc]


def _with(*changes: rf.Reviewer) -> tuple[rf.Reviewer, ...]:
    return (*rf.REVIEWERS, *changes)


def _replace(who: str, **fields) -> tuple[rf.Reviewer, ...]:
    return tuple(dataclasses.replace(r, **fields) if r.login == who else r for r in rf.REVIEWERS)


@pytest.mark.parametrize(
    "reviewers",
    [
        pytest.param(_replace(CODEX, primary=False), id="no-primary"),
        pytest.param(_replace(DEVIN, primary=True), id="two-primaries"),
        pytest.param(
            (rf.Reviewer(login=CODEQL, parser="codeql", primary=True, surface_only=True),),
            id="surface-only-primary",
        ),
        pytest.param(_with(rf.Reviewer(login=DEVIN, parser="devin-marker")), id="duplicate-login"),
        # A HUMAN account can never carry `[bot]`; naming one could let a session make
        # its own reviews satisfy merge freshness.
        pytest.param(_replace(CODEX, login="octocat"), id="human-login"),
        pytest.param(_replace(RABBIT, login="CodeRabbitAI[bot]"), id="uppercase-login"),
        pytest.param(_replace(RABBIT, login="<b>evil</b>[bot]"), id="markup-login"),
        pytest.param(_replace(RABBIT, login="a" * 40 + "[bot]"), id="overlong-login"),
        pytest.param(_replace(DEVIN, parser="guesswork"), id="unregistered-parser"),
        pytest.param(_replace(CODEQL, surface_only=False), id="codeql-not-surface-only"),
        pytest.param(_replace(RABBIT, surface_only=True), id="surface-only-scored-parser"),
        pytest.param(
            (rf.Reviewer(login=CODEQL, parser="codeql", surface_only=True),),
            id="only-surface-only",
        ),
        # The round counter's clean-comment match, the `@codex review` request gate
        # and every freshness message are Codex-specific.
        pytest.param(
            tuple(dataclasses.replace(r, primary=r.login == DEVIN) for r in rf.REVIEWERS),
            id="primary-not-codex-badge",
        ),
    ],
)
def test_validate_rejects_a_list_that_breaks_an_invariant(reviewers):
    """An edit that breaks an invariant fails at import, which the merge gate's soft
    import turns into a merge gate that blocks naming the error."""
    with pytest.raises(ValueError):
        rf._validate(reviewers)


def test_a_primary_on_another_parser_names_the_codex_badge_rule():
    moved = tuple(dataclasses.replace(r, primary=r.login == DEVIN) for r in rf.REVIEWERS)
    with pytest.raises(ValueError, match="the primary must use the codex-badge parser"):
        rf._validate(moved)


def test_accessors_on_the_shipped_list():
    assert rf.primary_reviewer_login() == CODEX
    assert rf.substitute_reviewer_logins() == (DEVIN, RABBIT)
    assert rf.enforced_logins() == {
        "codex-badge": frozenset({CODEX}),
        "devin-marker": frozenset({DEVIN}),
        "coderabbit-header": frozenset({RABBIT}),
        "codeql": frozenset({CODEQL}),
    }


def test_accessors_follow_the_list(monkeypatch):
    """Every reviewer the list names has its findings read, with its parser; a
    reviewer added to the list is enforced like a shipped one."""
    monkeypatch.setattr(
        rf, "REVIEWERS", _with(rf.Reviewer(login="acme-review[bot]", parser="devin-marker"))
    )
    sets = rf.enforced_logins()
    assert sets["devin-marker"] == {DEVIN, "acme-review[bot]"}
    assert sets["coderabbit-header"] == {RABBIT}
    assert rf.substitute_reviewer_logins() == (DEVIN, RABBIT, "acme-review[bot]")
    # Moving the primary moves freshness AND the substitute set with it: Codex,
    # demoted, becomes a possible stand-in.
    monkeypatch.setattr(rf, "REVIEWERS", _moved_primary())
    assert rf.primary_reviewer_login() == ACME_CODEX
    assert rf.substitute_reviewer_logins() == (CODEX, DEVIN, RABBIT, "acme-review[bot]")


#: A second codex-badge reviewer: the only kind the primary may move to.
ACME_CODEX = "acme-codex[bot]"


def _moved_primary() -> tuple[rf.Reviewer, ...]:
    """The current list with Codex demoted and ACME_CODEX added as primary."""
    moved = (
        *(dataclasses.replace(r, primary=False) for r in rf.REVIEWERS),
        rf.Reviewer(login=ACME_CODEX, parser="codex-badge", primary=True),
    )
    rf._validate(moved)
    return moved


# ── the merge gate reads the list ─────────────────────────────────────


@pytest.fixture
def guard():
    sys.path.insert(0, str(_ROOT / "scripts" / "hooks"))
    import git_push_guard

    assert sys.modules[git_push_guard.enforced_logins.__module__] is rf
    return git_push_guard


def _reviews(monkeypatch, *records):
    monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", "\n".join(json.dumps(r) for r in records))


def test_freshness_reads_the_primary(guard, monkeypatch):
    _reviews(
        monkeypatch,
        {"login": CODEX, "commit_id": "b" * 40, "state": "COMMENTED"},
        {"login": ACME_CODEX, "commit_id": H, "state": "COMMENTED"},
    )
    assert guard._codex_reviews("1") == [{"commit_id": "b" * 40, "state": "COMMENTED"}]
    # Move the primary: the same records now read as the new primary's.
    monkeypatch.setattr(rf, "REVIEWERS", _moved_primary())
    assert guard._codex_reviews("1") == [{"commit_id": H, "state": "COMMENTED"}]


def test_an_unimportable_list_is_an_error_not_an_empty_review_list(guard, monkeypatch):
    """None (error) blocks as unreadable; [] would read as 'no review yet'."""
    _reviews(monkeypatch, {"login": CODEX, "commit_id": H, "state": "COMMENTED"})
    monkeypatch.setattr(guard, "primary_reviewer_login", lambda: None)
    assert guard._codex_reviews("1") is None


def test_substitutes_come_from_the_list(guard, monkeypatch):
    _reviews(
        monkeypatch,
        {"login": DEVIN, "commit_id": H, "state": "COMMENTED", "has_body": True},
        {"login": RABBIT, "commit_id": H, "state": "COMMENTED", "has_body": True},
    )
    assert guard._substitute_reviewers_at_head("1", H) == ([DEVIN, RABBIT], [])
    # A list without Devin: only CodeRabbit may stand in.
    monkeypatch.setattr(rf, "REVIEWERS", tuple(r for r in rf.REVIEWERS if r.login != DEVIN))
    assert guard._substitute_reviewers_at_head("1", H) == ([RABBIT], [])
    # An unimportable list is "could not evaluate", never "no substitute found".
    monkeypatch.setattr(guard, "_REVIEW_FINDINGS_ERROR", "review_findings is broken")
    assert guard._substitute_reviewers_at_head("1", H) is None


def test_freshness_names_an_unimportable_list_instead_of_a_missing_review(guard, monkeypatch):
    """A broken hook tree must not read as 'no Codex review found' and send the
    operator to re-request a review that cannot help."""
    monkeypatch.setenv("_TEST_GH_HEAD_SHA", H)
    _reviews(monkeypatch, {"login": CODEX, "commit_id": H, "state": "COMMENTED"})
    monkeypatch.setattr(guard, "_REVIEW_FINDINGS_ERROR", "review_findings is broken")
    block, msg, head = guard._check_codex_reviewed_head("1")
    assert block is True and head is None
    assert "the hook tree is broken: review_findings is broken" in msg


def test_freshness_block_on_the_shipped_list_names_codex(guard, monkeypatch):
    monkeypatch.setenv("_TEST_GH_HEAD_SHA", H)
    monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", "")
    block, msg, _ = guard._check_codex_reviewed_head("1")
    assert block is True
    assert "@codex review" in msg
    assert "hook tree is broken" not in msg


def test_the_moved_parsers_are_the_ones_the_gate_uses(guard):
    """One implementation: the guard's names ARE the shared module's objects."""
    assert guard._cr_severity is rf.cr_severity
    assert guard._devin_finding is rf.devin_finding
    assert guard._INLINE_P1_RE is rf.INLINE_P1_RE
    assert guard._INLINE_P2_RE is rf.INLINE_P2_RE
    assert guard._CR_HEADER_FIELD_RE is rf.CR_HEADER_FIELD_RE
    assert guard._CR_SEVERITIES is rf.CR_SEVERITIES


def test_the_guard_keeps_no_second_copy_of_the_reviewer_logins():
    """The logins live only in the list: a hardcoded copy in the gate would let trust
    and enforcement drift apart again."""
    import re

    src = (_ROOT / "scripts" / "hooks" / "git_push_guard.py").read_text(encoding="utf-8")
    for name in (
        "_CODEX_REVIEW_BOT",
        "_SUBSTITUTE_REVIEW_LOGINS",
        "_REVIEW_BOTS",
        "_CODERABBIT_LOGINS",
        "_DEVIN_LOGINS",
        "_DEVIN_MARKERS",
        "_INLINE_REVIEW_BOTS",
    ):
        assert not re.search(rf"^{name}\b", src, re.M), name


def test_no_script_string_names_a_listed_reviewer():
    """Catches a copy under ANY name, in ANY script, not only the retired ones: no
    string VALUE under scripts/ may carry a listed reviewer's login, so trust cannot
    drift from the list. Comments and docstrings are documentation, not values, and
    are not scanned. Exactly one exception: `review_budget.CODEX_REVIEW_BOT`, the
    pure evaluator's default, pinned equal to the list's primary by the test below."""
    import ast

    logins = [r.login for r in rf.REVIEWERS]
    hits = []
    for path in sorted((_ROOT / "scripts").rglob("*.py")):
        rel = path.relative_to(_ROOT).as_posix()
        if rel == "scripts/review_findings.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        skipped = {
            id(node.value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
        }
        if rel == "scripts/review_budget.py":
            skipped |= {
                id(node.value)
                for node in ast.walk(tree)
                if isinstance(node, ast.Assign)
                and [getattr(t, "id", None) for t in node.targets] == ["CODEX_REVIEW_BOT"]
            }
        hits += [
            (rel, node.lineno, login)
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in skipped
            for login in logins
            if login in node.value
        ]
    assert hits == []


def test_the_round_counters_default_is_the_lists_primary():
    """`review_budget.CODEX_REVIEW_BOT` is only the pure evaluator's default (the live
    path passes the list's primary); it must never disagree with the list."""
    import review_budget as rb

    assert rf.primary_reviewer_login() == rb.CODEX_REVIEW_BOT
