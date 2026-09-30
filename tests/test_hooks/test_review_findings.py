"""The reviewer table (config/reviewers.yaml + install overlay) and its readers."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "scripts"))

import review_findings as rf  # noqa: E402

CODEX = "chatgpt-codex-connector[bot]"
DEVIN = "devin-ai-integration[bot]"
RABBIT = "coderabbitai[bot]"
SHIPPED = f"reviewers:\n  {CODEX}: primary parser=codex-badge floor=P1 minor=P2 analysis=P3\n"


def _table(monkeypatch, shipped=SHIPPED, overlay=""):
    monkeypatch.setenv("_TEST_REVIEWERS_YAML", shipped)
    monkeypatch.setenv("_TEST_REVIEWERS_LOCAL_YAML", overlay)
    return rf.configured_reviewers()


def test_the_shipped_file_parses_and_names_codex_primary(monkeypatch):
    """The real file in the tree, read with no overlay: the review apps installed
    on the repository, Codex primary, CodeQL shown but never standing in."""
    monkeypatch.delenv("_TEST_REVIEWERS_YAML", raising=False)
    monkeypatch.setenv("_TEST_REVIEWERS_LOCAL_YAML", "")
    table, error = rf.configured_reviewers()
    assert error is None, error
    assert list(table) == [CODEX, DEVIN, RABBIT, "github-advanced-security[bot]"]
    assert table["github-advanced-security[bot]"].surface_only is True
    assert table[RABBIT].body_findings is True
    assert table[CODEX] == rf.Reviewer(
        login=CODEX,
        parser="codex-badge",
        primary=True,
        floor=("P1",),
        minor=("P2",),
        analysis=("P3",),
    )
    assert rf.primary_reviewer_login() == CODEX
    assert rf.substitute_reviewer_logins() == (DEVIN, RABBIT)


def test_overlay_adds_reviewers_in_file_order(monkeypatch):
    overlay = (
        "# local reviewers\n"
        "reviewers:\n"
        f"  {DEVIN}: parser=devin-marker floor=🔴,🟥 minor=🟡,🟨 analysis=🔍  # a comment\n"
        f"  {RABBIT}: body-findings parser=coderabbit-header floor=critical,major\n"
        "  github-advanced-security[bot]: surface-only parser=codeql\n"
    )
    table, error = _table(monkeypatch, overlay=overlay)
    assert error is None, error
    assert list(table) == [CODEX, DEVIN, RABBIT, "github-advanced-security[bot]"]
    assert table[DEVIN].floor == ("🔴", "🟥") and table[DEVIN].analysis == ("🔍",)
    assert table[RABBIT].body_findings is True
    # Surface-only reviewers never stand in for the primary.
    assert rf.substitute_reviewer_logins() == (DEVIN, RABBIT)


def test_overlay_line_replaces_the_shipped_line_and_can_move_primary(monkeypatch):
    overlay = (
        "reviewers:\n"
        f"  {CODEX}: parser=codex-badge floor=P1\n"
        f"  {DEVIN}: primary parser=devin-marker floor=🔴\n"
    )
    table, error = _table(monkeypatch, overlay=overlay)
    assert error is None, error
    assert rf.primary_reviewer_login() == DEVIN
    assert table[CODEX].minor == (), "the overlay line REPLACES the shipped one"
    assert rf.substitute_reviewer_logins() == (CODEX,)


def test_disabled_removes_a_reviewer(monkeypatch):
    shipped = SHIPPED + f"  {DEVIN}: parser=devin-marker\n"
    table, error = _table(
        monkeypatch, shipped=shipped, overlay=f"reviewers:\n  {DEVIN}: disabled\n"
    )
    assert error is None, error
    assert list(table) == [CODEX]


@pytest.mark.parametrize(
    ("shipped", "overlay"),
    [
        pytest.param(
            SHIPPED + f"  {DEVIN}: parser=devin-marker\n  {DEVIN}: parser=devin-marker\n",
            "",
            id="duplicate-login",
        ),
        pytest.param(SHIPPED + "  just-a-word\n", "", id="not-key-value"),
        pytest.param(SHIPPED + f"  {DEVIN}: parser=devin-marker loud\n", "", id="unknown-flag"),
        pytest.param(
            SHIPPED + f"  {DEVIN}: body-findings body-findings parser=devin-marker\n",
            "",
            id="repeated-flag",
        ),
        pytest.param(
            SHIPPED + f"  {DEVIN}: parser=devin-marker colour=red\n", "", id="unknown-key"
        ),
        pytest.param(
            SHIPPED + f"  {DEVIN}: parser=devin-marker parser=codeql\n", "", id="repeated-key"
        ),
        pytest.param(SHIPPED + f"  {DEVIN}: parser=devin-marker floor=\n", "", id="empty-value"),
        pytest.param(SHIPPED + f"  {DEVIN}: floor=🔴\n", "", id="missing-parser"),
        pytest.param(SHIPPED + f"  {DEVIN}: parser=guesswork\n", "", id="unregistered-parser"),
        pytest.param(SHIPPED + "  <b>evil</b>: parser=codex-badge\n", "", id="not-a-login"),
        # A HUMAN account can never carry `[bot]`; naming one could let a session make
        # its own reviews satisfy merge freshness.
        pytest.param(
            f"reviewers:\n  {CODEX}: parser=codex-badge\n  octocat: primary parser=codex-badge\n",
            "",
            id="human-login",
        ),
        pytest.param(SHIPPED + "  CodeRabbitAI[bot]: disabled\n", "", id="uppercase-login"),
        pytest.param(SHIPPED + "other:\n  x: y\n", "", id="second-block"),
        pytest.param(f"  {CODEX}: primary parser=codex-badge\n", "", id="entry-outside-block"),
        pytest.param(f"reviewers:\n  {CODEX}: parser=codex-badge\n", "", id="no-primary"),
        pytest.param(
            SHIPPED, f"reviewers:\n  {DEVIN}: primary parser=devin-marker\n", id="two-primaries"
        ),
        pytest.param(
            f"reviewers:\n  {CODEX}: primary surface-only parser=codex-badge\n",
            "",
            id="surface-only-primary",
        ),
        pytest.param(SHIPPED, f"reviewers:\n  {CODEX}: disabled\n", id="primary-disabled"),
    ],
)
def test_malformed_config_is_unknown_never_a_partial_table(monkeypatch, shipped, overlay):
    """A typo must never silently drop a reviewer: the whole table is unknown,
    so every consumer treats review evidence as unreadable."""
    table, error = _table(monkeypatch, shipped=shipped, overlay=overlay)
    assert table is None
    assert error == "reviewers_config_malformed"
    assert rf.primary_reviewer_login() is None
    assert rf.substitute_reviewer_logins() == ()


def test_a_comment_needs_whitespace_before_the_hash(monkeypatch):
    """`#` inside a word is data, as in YAML; only ` #` starts a comment."""
    shipped = f"reviewers:\n  {CODEX}: primary parser=codex-badge floor=P#1  # trailing\n"
    table, error = _table(monkeypatch, shipped=shipped)
    assert error is None, error
    assert table[CODEX].floor == ("P#1",)


def test_missing_or_unreadable_shipped_file_is_unknown(monkeypatch, tmp_path):
    monkeypatch.delenv("_TEST_REVIEWERS_YAML", raising=False)
    monkeypatch.setenv("_TEST_REVIEWERS_LOCAL_YAML", "")
    monkeypatch.setattr(rf, "_SHIPPED", tmp_path / "absent.yaml")
    assert rf.configured_reviewers() == (None, "reviewers_config_missing")
    monkeypatch.setattr(rf, "_SHIPPED", tmp_path)  # a directory: read_text raises
    assert rf.configured_reviewers() == (None, "reviewers_config_unreadable")
    assert rf.primary_reviewer_login() is None


def test_the_overlay_file_is_read_when_no_seam_is_set(monkeypatch, tmp_path):
    overlay = tmp_path / "reviewers.local.yaml"
    overlay.write_text(f"reviewers:\n  {DEVIN}: parser=devin-marker\n")
    monkeypatch.setenv("_TEST_REVIEWERS_YAML", SHIPPED)
    monkeypatch.delenv("_TEST_REVIEWERS_LOCAL_YAML", raising=False)
    monkeypatch.setattr(rf, "_OVERLAY", overlay)
    assert rf.substitute_reviewer_logins() == (DEVIN,)
    monkeypatch.setattr(rf, "_OVERLAY", tmp_path / "absent.yaml")
    assert rf.substitute_reviewer_logins() == ()


# ── the merge gate reads the table ────────────────────────────────────


@pytest.fixture
def guard():
    sys.path.insert(0, str(_ROOT / "scripts" / "hooks"))
    import git_push_guard

    return git_push_guard


def _reviews(monkeypatch, *records):
    import json

    monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", "\n".join(json.dumps(r) for r in records))


H = "a" * 40


def test_freshness_reads_the_configured_primary(guard, monkeypatch):
    _reviews(
        monkeypatch,
        {"login": CODEX, "commit_id": "b" * 40, "state": "COMMENTED"},
        {"login": DEVIN, "commit_id": H, "state": "COMMENTED"},
    )
    assert guard._codex_reviews("1") == [{"commit_id": "b" * 40, "state": "COMMENTED"}]
    # Make Devin the primary: the same records now read as Devin's.
    monkeypatch.setenv("_TEST_REVIEWERS_YAML", f"reviewers:\n  {CODEX}: parser=codex-badge\n")
    monkeypatch.setenv(
        "_TEST_REVIEWERS_LOCAL_YAML", f"reviewers:\n  {DEVIN}: primary parser=devin-marker\n"
    )
    assert guard._codex_reviews("1") == [{"commit_id": H, "state": "COMMENTED"}]


def test_an_unreadable_table_is_an_error_not_an_empty_review_list(guard, monkeypatch):
    """None (error) blocks as unreadable; [] would read as 'no review yet'."""
    _reviews(monkeypatch, {"login": CODEX, "commit_id": H, "state": "COMMENTED"})
    monkeypatch.setenv("_TEST_REVIEWERS_YAML", "garbage")
    assert guard._codex_reviews("1") is None


def test_substitutes_come_from_the_table(guard, monkeypatch):
    _reviews(
        monkeypatch,
        {"login": DEVIN, "commit_id": H, "state": "COMMENTED", "has_body": True},
        {"login": RABBIT, "commit_id": H, "state": "COMMENTED", "has_body": True},
    )
    assert guard._substitute_reviewers_at_head("1", H) == ([DEVIN, RABBIT], [])
    monkeypatch.setenv("_TEST_REVIEWERS_LOCAL_YAML", f"reviewers:\n  {DEVIN}: disabled\n")
    assert guard._substitute_reviewers_at_head("1", H) == ([RABBIT], [])
    # An unreadable table is "could not evaluate", never "no substitute found".
    monkeypatch.setenv("_TEST_REVIEWERS_YAML", "garbage")
    assert guard._substitute_reviewers_at_head("1", H) is None


def test_freshness_names_an_unreadable_table_instead_of_a_missing_review(guard, monkeypatch):
    """A typo in the overlay must not read as 'no Codex review found' and send the
    operator to re-request a review that cannot help."""
    monkeypatch.setenv("_TEST_GH_HEAD_SHA", H)
    _reviews(monkeypatch, {"login": CODEX, "commit_id": H, "state": "COMMENTED"})
    monkeypatch.setenv("_TEST_REVIEWERS_LOCAL_YAML", f"reviewers:\n  {CODEX}: disabled\n")
    block, msg, head = guard._check_codex_reviewed_head("1")
    assert block is True and head is None
    assert "reviewer table is unreadable (reviewers_config_malformed)" in msg
    assert "reviewers.local.yaml" in msg


def test_the_moved_parsers_are_the_ones_the_gate_uses(guard):
    """One implementation: the guard's names ARE the shared module's objects."""
    assert guard._cr_severity is rf.cr_severity
    assert guard._devin_finding is rf.devin_finding
    assert guard._INLINE_P1_RE is rf.INLINE_P1_RE
