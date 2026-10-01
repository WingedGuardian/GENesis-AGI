"""Who reviews (`review_findings`): TRUST is the named primary plus eligible
stand-ins; ENFORCEMENT is the known-format registry. Reviewed code, no config."""

from __future__ import annotations

import ast
import dataclasses
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "scripts"))

import review_findings as rf  # noqa: E402

CODEX = "chatgpt-codex-connector[bot]"
DEVIN = "devin-ai-integration[bot]"
RABBIT = "coderabbitai[bot]"
CODEQL = "github-advanced-security[bot]"
ACTIONS = "github-actions[bot]"
ACME_CODEX = "acme-codex[bot]"
H = "a" * 40
_GUARD = _ROOT / "scripts" / "hooks" / "git_push_guard.py"


# ── the module's shipped definitions ─────────────────────────────────


def test_the_primary_is_the_codex_app_and_workflow_bots_are_named():
    assert rf.CODEX_LOGIN == CODEX
    assert rf.primary_reviewer_login() == CODEX
    assert {ACTIONS} == rf.WORKFLOW_BOTS


def test_the_shipped_known_formats_in_order():
    """Which parser reads which known login — and nothing about trust."""
    assert (
        rf.Reviewer(login=CODEX, parser="codex-badge"),
        rf.Reviewer(login=DEVIN, parser="devin-marker"),
        rf.Reviewer(login=RABBIT, parser="coderabbit-header"),
        rf.Reviewer(login=CODEQL, parser="codeql", surface_only=True),
    ) == rf.KNOWN_FORMATS
    assert [f.name for f in dataclasses.fields(rf.Reviewer)] == [
        "login",
        "parser",
        "surface_only",
    ]
    rf._validate(rf.KNOWN_FORMATS)  # and it passes its own invariants


def test_known_formats_are_immutable():
    with pytest.raises(dataclasses.FrozenInstanceError):
        rf.KNOWN_FORMATS[0].parser = "codeql"  # type: ignore[misc]


def _with(*extra: rf.Reviewer) -> tuple[rf.Reviewer, ...]:
    return (*rf.KNOWN_FORMATS, *extra)


def _replace(who: str, **fields) -> tuple[rf.Reviewer, ...]:
    return tuple(
        dataclasses.replace(r, **fields) if r.login == who else r for r in rf.KNOWN_FORMATS
    )


@pytest.mark.parametrize(
    ("formats", "message"),
    [
        pytest.param(
            tuple(r for r in rf.KNOWN_FORMATS if r.login != CODEX),
            "the primary must be listed with the codex-badge parser",
            id="primary-missing",
        ),
        pytest.param(
            _replace(CODEX, parser="devin-marker"),
            "the primary must be listed with the codex-badge parser",
            id="primary-not-codex-badge",
        ),
        # A bot the PR's own workflow drives is the PR author speaking.
        pytest.param(
            _with(rf.Reviewer(login=ACTIONS, parser="codex-badge")),
            "a workflow bot is never a reviewer",
            id="workflow-bot-listed",
        ),
        pytest.param(
            _with(rf.Reviewer(login=DEVIN, parser="devin-marker")),
            "listed twice",
            id="duplicate-login",
        ),
        # A HUMAN account can never carry `[bot]`.
        pytest.param(
            _replace(DEVIN, login="octocat"), "not a GitHub App REST login", id="human-login"
        ),
        pytest.param(
            _replace(RABBIT, login="CodeRabbitAI[bot]"),
            "not a GitHub App REST login",
            id="uppercase-login",
        ),
        pytest.param(
            _replace(RABBIT, login="<b>evil</b>[bot]"),
            "not a GitHub App REST login",
            id="markup-login",
        ),
        pytest.param(
            _replace(RABBIT, login="a" * 40 + "[bot]"),
            "not a GitHub App REST login",
            id="overlong-login",
        ),
        pytest.param(
            _replace(DEVIN, parser="guesswork"), "unregistered parser", id="unregistered-parser"
        ),
        pytest.param(
            _replace(CODEQL, surface_only=False),
            "surface_only goes with parser 'codeql'",
            id="codeql-not-surface-only",
        ),
        pytest.param(
            _replace(RABBIT, surface_only=True),
            "surface_only goes with parser 'codeql'",
            id="surface-only-scored-parser",
        ),
    ],
)
def test_validate_rejects_a_registry_that_breaks_an_invariant(formats, message):
    """An edit that breaks an invariant fails at import, which the merge gate's soft
    import turns into a merge gate that blocks."""
    with pytest.raises(ValueError, match=message):
        rf._validate(formats)


def _parsers_the_guard_reads() -> set[str]:
    """Every parser name the merge gate's scanners dispatch on: each
    ``scanner_sets.get("<name>", …)`` literal, plus ``_INLINE_REVIEW_PARSERS``."""
    tree = ast.parse(_GUARD.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "scanner_sets"
            and node.args
            and isinstance(node.args[0], ast.Constant)
        ):
            names.add(node.args[0].value)
        if isinstance(node, ast.Assign) and [getattr(t, "id", None) for t in node.targets] == [
            "_INLINE_REVIEW_PARSERS"
        ]:
            names |= {e.value for e in node.value.elts}  # type: ignore[attr-defined]
    return names


def test_every_registered_parser_is_one_a_scanner_reads():
    """Codex P2 (#2683): a parser name no scanner dispatches on would make a known
    reviewer's findings silently unread. Registration must equal consumption."""
    assert _parsers_the_guard_reads() == rf.PARSERS


def test_accessors_on_the_shipped_registry():
    assert rf.enforced_logins() == {
        "codex-badge": frozenset({CODEX}),
        "devin-marker": frozenset({DEVIN}),
        "coderabbit-header": frozenset({RABBIT}),
        "codeql": frozenset({CODEQL}),
    }
    assert {CODEQL} == rf.surface_only_logins()
    assert not hasattr(rf, "substitute_reviewer_logins")
    assert not hasattr(rf, "REVIEWERS")


def test_enforcement_follows_the_registry(monkeypatch):
    """A login added to the registry is read with its parser — and gains no trust
    by being there."""
    monkeypatch.setattr(
        rf, "KNOWN_FORMATS", _with(rf.Reviewer(login="acme-review[bot]", parser="devin-marker"))
    )
    sets = rf.enforced_logins()
    assert sets["devin-marker"] == {DEVIN, "acme-review[bot]"}
    assert sets["coderabbit-header"] == {RABBIT}
    assert rf.primary_reviewer_login() == CODEX


@pytest.mark.parametrize(
    ("login", "user_type", "expected"),
    [
        pytest.param(DEVIN, "Bot", True, id="devin"),
        pytest.param(RABBIT, "Bot", True, id="coderabbit"),
        pytest.param("acme-review[bot]", "Bot", True, id="unlisted-app-needs-no-entry"),
        pytest.param(CODEX, "Bot", False, id="the-primary-itself"),
        pytest.param(ACTIONS, "Bot", False, id="workflow-bot"),
        pytest.param(CODEQL, "Bot", False, id="surface-only"),
        pytest.param("octocat", "User", False, id="human"),
        pytest.param("acme-review[bot]", "User", False, id="not-bot-typed"),
        pytest.param("Acme[bot]", "Bot", False, id="not-an-app-login"),
        pytest.param("<b>x</b>[bot]", "Bot", False, id="markup"),
    ],
)
def test_is_substitute_candidate(login, user_type, expected):
    assert rf.is_substitute_candidate(login, user_type) is expected


# ── the merge gate reads it ──────────────────────────────────────────


@pytest.fixture
def guard():
    sys.path.insert(0, str(_ROOT / "scripts" / "hooks"))
    import git_push_guard

    assert sys.modules[git_push_guard.enforced_logins.__module__] is rf
    return git_push_guard


def _reviews(monkeypatch, *records):
    monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", "\n".join(json.dumps(r) for r in records))


def test_freshness_reads_the_primary_constant(guard, monkeypatch):
    _reviews(
        monkeypatch,
        {"login": CODEX, "commit_id": "b" * 40, "state": "COMMENTED"},
        {"login": ACME_CODEX, "commit_id": H, "state": "COMMENTED"},
    )
    assert guard._codex_reviews("1") == [{"commit_id": "b" * 40, "state": "COMMENTED"}]
    # Bound to the constant, not a copy: moving it moves freshness and the
    # substitute exclusion with it.
    monkeypatch.setattr(rf, "CODEX_LOGIN", ACME_CODEX)
    assert guard._codex_reviews("1") == [{"commit_id": H, "state": "COMMENTED"}]
    assert rf.is_substitute_candidate(CODEX, "Bot") is True
    assert rf.is_substitute_candidate(ACME_CODEX, "Bot") is False


def test_an_unimportable_module_is_an_error_not_an_empty_review_list(guard, monkeypatch):
    """None (error) blocks as unreadable; [] would read as 'no review yet'."""
    _reviews(monkeypatch, {"login": CODEX, "commit_id": H, "state": "COMMENTED"})
    monkeypatch.setattr(guard, "primary_reviewer_login", lambda: None)
    assert guard._codex_reviews("1") is None


def test_a_clean_signal_is_never_accepted_without_the_primary(guard, monkeypatch):
    """The clean-signal check reads the primary from the module too: with it
    unavailable it must refuse, not fall back to a hardcoded login."""
    monkeypatch.setattr(guard, "primary_reviewer_login", lambda: None)
    monkeypatch.setattr(guard, "_codex_signal_evidence", lambda *a, **k: pytest.fail("read"))
    assert guard._codex_clean_signal_at_head("1", H) is None


def _rec(login, *, user_type="Bot", commit=H, state="COMMENTED", has_body=True):
    return {
        "login": login,
        "type": user_type,
        "commit_id": commit,
        "state": state,
        "has_body": has_body,
    }


def test_substitutes_are_any_eligible_bot_reviewer_at_head(guard, monkeypatch):
    """Owner ruling 2026-09-30: a new reviewer needs no list entry to be offered as
    a stand-in; the exclusions are what keep the offer trustworthy."""
    _reviews(
        monkeypatch,
        _rec(RABBIT),
        _rec(ACTIONS),  # the PR's own workflow
        _rec(CODEQL),  # surface-only
        _rec("octocat", user_type="User"),  # a human
        _rec(CODEX),  # the primary is not its own stand-in
        _rec("wrapper-bot[bot]", has_body=False),  # a thread-reply wrapper
        _rec("dismissed-bot[bot]", state="DISMISSED"),
        _rec("pending-bot[bot]", state="PENDING"),
        _rec("acme-review[bot]"),  # unlisted: offered
        _rec(DEVIN),
        _rec(DEVIN),  # a second review by the same bot is not a second stand-in
        _rec("elsewhere-bot[bot]", commit="c" * 40),
    )
    at_head, elsewhere = guard._substitute_reviewers_at_head("1", H)
    assert at_head == [RABBIT, "acme-review[bot]", DEVIN]  # first appearance
    assert elsewhere == [("elsewhere-bot[bot]", "c" * 40)]


def test_a_seam_record_without_a_type_is_typed_by_its_login(guard, monkeypatch):
    """Records with no `type` (the seam, an older projection) read as a Bot exactly
    when the login carries `[bot]`, which no human login can."""
    _reviews(
        monkeypatch,
        {"login": DEVIN, "commit_id": H, "state": "COMMENTED"},
        {"login": "octocat", "commit_id": H, "state": "COMMENTED"},
    )
    assert guard._substitute_reviewers_at_head("1", H) == ([DEVIN], [])
    # And the projection asks GitHub for the type, so live records never rely on it.
    assert "type: .user.type" in _GUARD.read_text(encoding="utf-8")


def test_an_unimportable_module_offers_no_substitute(guard, monkeypatch):
    _reviews(monkeypatch, _rec(DEVIN))
    monkeypatch.setattr(guard, "_REVIEW_FINDINGS_ERROR", "review_findings is broken")
    assert guard._substitute_reviewers_at_head("1", H) is None


def test_freshness_names_an_unimportable_module_instead_of_a_missing_review(guard, monkeypatch):
    """A broken hook tree must not read as 'no Codex review found' and send the
    operator to re-request a review that cannot help."""
    monkeypatch.setenv("_TEST_GH_HEAD_SHA", H)
    _reviews(monkeypatch, {"login": CODEX, "commit_id": H, "state": "COMMENTED"})
    monkeypatch.setattr(guard, "_REVIEW_FINDINGS_ERROR", "review_findings is broken")
    block, msg, head = guard._check_codex_reviewed_head("1")
    assert block is True and head is None
    assert "the hook tree is broken: review_findings is broken" in msg


def test_freshness_block_on_the_shipped_set_names_codex(guard, monkeypatch):
    monkeypatch.setenv("_TEST_GH_HEAD_SHA", H)
    monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", "")
    block, msg, _ = guard._check_codex_reviewed_head("1")
    assert block is True
    assert "@codex review" in msg
    assert "accept another reviewer's review AT THIS HEAD" in msg
    assert "hook tree is broken" not in msg


def _body_scan(guard, comments):
    with patch.object(
        guard.subprocess,
        "run",
        return_value=subprocess.CompletedProcess(
            [], 0, "\n".join(json.dumps(c) for c in comments), ""
        ),
    ):
        return guard._check_pr_review_findings("1")


@pytest.fixture
def second_badge_reviewer(monkeypatch):
    monkeypatch.setattr(
        rf, "KNOWN_FORMATS", _with(rf.Reviewer(login=ACME_CODEX, parser="codex-badge"))
    )
    return ACME_CODEX


_P1 = "[P1] a real bug"
_PASS = "VERDICT: PASS"


def test_a_second_badge_bots_clean_verdict_does_not_clear_the_primarys_finding(
    guard, second_badge_reviewer
):
    """Devin 🔴 / Codex P2 (#2683): each login's newest verdict is its own. Another
    badge-format bot posting `VERDICT: PASS` after the primary's `[P1]` is not the
    primary resolving its finding."""
    block, msg = _body_scan(
        guard,
        [{"login": CODEX, "body": _P1}, {"login": second_badge_reviewer, "body": _PASS}],
    )
    assert block is True and "unresolved findings" in msg


def test_a_second_badge_bots_finding_is_not_cleared_by_the_primarys_pass(
    guard, second_badge_reviewer
):
    """The mirror: a known badge reviewer's own `[P1]` is READ (enforcement is open
    per reviewer), and the primary's later clean verdict does not clear it."""
    block, msg = _body_scan(
        guard,
        [{"login": second_badge_reviewer, "body": _P1}, {"login": CODEX, "body": _PASS}],
    )
    assert block is True and "unresolved findings" in msg
    # Alone, too: its finding is never ignored for not being the primary's.
    block, _ = _body_scan(guard, [{"login": second_badge_reviewer, "body": _P1}])
    assert block is True


def test_each_logins_own_newest_pass_supersedes_its_own_older_finding(
    guard, second_badge_reviewer
):
    """Control: per-login does not mean sticky. Each reviewer's own newer clean
    verdict supersedes its own older finding, interleaved or not."""
    comments = [
        {"login": CODEX, "body": _P1},
        {"login": second_badge_reviewer, "body": _P1},
        {"login": CODEX, "body": _PASS},
        {"login": second_badge_reviewer, "body": _PASS},
    ]
    assert _body_scan(guard, comments) == (False, "")
    # And an unknown author's verdict-shaped comment is still not a verdict.
    assert _body_scan(guard, [*comments, {"login": "someone[bot]", "body": _P1}]) == (False, "")


def test_the_walk_is_unchanged_for_the_primary_alone(guard):
    """Byte-identical on the shipped set: the primary's newest verdict decides."""
    assert _body_scan(guard, [{"login": CODEX, "body": _PASS}, {"login": CODEX, "body": _P1}])[0]
    assert _body_scan(guard, [{"login": CODEX, "body": _P1}, {"login": CODEX, "body": _PASS}]) == (
        False,
        "",
    )


_UNKNOWN_NOTE = " (format unknown: its findings are not scored yet)"


def test_an_unknown_format_stand_in_is_named_as_unscored(guard):
    """Informed consent: the owner approves a stand-in knowing whether anything
    scores its findings. Known formats read exactly as before."""
    assert guard._substitute_label(DEVIN) == DEVIN
    assert guard._substitute_label(RABBIT) == RABBIT
    assert guard._substitute_label("acme-review[bot]") == "acme-review[bot]" + _UNKNOWN_NOTE


def test_the_check_pr_row_names_an_unknown_format_stand_in(guard, monkeypatch):
    monkeypatch.setenv("_TEST_GH_HEAD_SHA", H)
    _reviews(monkeypatch, _rec(DEVIN), _rec("acme-review[bot]"))
    assert guard._substitute_available_note("1") == (
        f" — substitute available: {DEVIN} and acme-review[bot]{_UNKNOWN_NOTE} reviewed "
        f"this head (with the owner's yes in conversation, merge with "
        f"'# substitute-review')"
    )
    _reviews(monkeypatch, _rec(DEVIN))
    assert guard._substitute_available_note("1") == (
        f" — substitute available: {DEVIN} reviewed this head (with the owner's yes in "
        f"conversation, merge with '# substitute-review')"
    )


def test_the_freshness_substitute_line_names_an_unknown_format_stand_in(guard, monkeypatch):
    monkeypatch.setenv("_TEST_GH_HEAD_SHA", H)
    stale = "c" * 40
    _reviews(monkeypatch, _rec(DEVIN, commit=stale), _rec("acme-review[bot]", commit=stale))
    block, msg, _ = guard._check_codex_reviewed_head("1", substitute=True)
    assert block is True
    assert (
        f"substitute reviews on other commits: {DEVIN}@{stale[:12]}, "
        f"acme-review[bot]@{stale[:12]}{_UNKNOWN_NOTE})"
    ) in msg


def test_the_moved_parsers_are_the_ones_the_gate_uses(guard):
    """One implementation: the guard's names ARE the shared module's objects."""
    assert guard._cr_severity is rf.cr_severity
    assert guard._devin_finding is rf.devin_finding
    assert guard._INLINE_P1_RE is rf.INLINE_P1_RE
    assert guard._INLINE_P2_RE is rf.INLINE_P2_RE
    assert guard._CR_HEADER_FIELD_RE is rf.CR_HEADER_FIELD_RE
    assert guard._CR_SEVERITIES is rf.CR_SEVERITIES


def test_the_guard_keeps_no_second_copy_of_the_reviewer_logins():
    """The logins live only in review_findings: a hardcoded copy in the gate would
    let trust and enforcement drift apart again."""
    import re

    src = _GUARD.read_text(encoding="utf-8")
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
    string VALUE under scripts/ or src/ may carry a known reviewer's login. Comments
    and docstrings are documentation, not values, and are not scanned. Exactly one
    exception: `review_budget.CODEX_REVIEW_BOT`, the pure evaluator's default,
    pinned equal to the primary by the test below."""
    logins = [r.login for r in rf.KNOWN_FORMATS]
    hits = []
    for path in sorted([*(_ROOT / "scripts").rglob("*.py"), *(_ROOT / "src").rglob("*.py")]):
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


def test_the_round_counters_default_is_the_primary():
    """`review_budget.CODEX_REVIEW_BOT` is only the pure evaluator's default (the live
    path passes the primary); it must never disagree with it."""
    import review_budget as rb

    assert rf.primary_reviewer_login() == rb.CODEX_REVIEW_BOT
