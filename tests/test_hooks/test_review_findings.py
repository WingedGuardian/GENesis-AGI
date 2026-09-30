"""The reviewer table (config/reviewers.yaml, shipped file only) and its readers."""

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


def _table(monkeypatch, shipped=SHIPPED):
    monkeypatch.setenv("_TEST_REVIEWERS_YAML", shipped)
    return rf.configured_reviewers()


def test_the_shipped_file_parses_and_names_codex_primary(monkeypatch):
    """The real file in the tree: the review apps installed on the repository,
    Codex primary, CodeQL shown but never standing in."""
    monkeypatch.delenv("_TEST_REVIEWERS_YAML", raising=False)
    table, error = rf.configured_reviewers()
    assert error is None, error
    assert list(table) == [CODEX, DEVIN, RABBIT, "github-advanced-security[bot]"]
    assert table["github-advanced-security[bot]"].surface_only is True
    assert table[RABBIT].body_findings is True
    assert table[DEVIN].floor == ("🔴", "🟥") and table[DEVIN].analysis == ("🔍",)
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


@pytest.mark.parametrize(
    "shipped",
    [
        pytest.param(
            SHIPPED + f"  {DEVIN}: parser=devin-marker\n  {DEVIN}: parser=devin-marker\n",
            id="duplicate-login",
        ),
        pytest.param(SHIPPED + "  just-a-word\n", id="not-key-value"),
        pytest.param(SHIPPED + f"  {DEVIN}: parser=devin-marker loud\n", id="unknown-flag"),
        pytest.param(
            SHIPPED + f"  {DEVIN}: body-findings body-findings parser=devin-marker\n",
            id="repeated-flag",
        ),
        pytest.param(
            SHIPPED + f"  {DEVIN}: parser=devin-marker colour=red\n", id="unknown-key"
        ),
        pytest.param(
            SHIPPED + f"  {DEVIN}: parser=devin-marker parser=codeql\n", id="repeated-key"
        ),
        pytest.param(SHIPPED + f"  {DEVIN}: parser=devin-marker floor=\n", id="empty-value"),
        pytest.param(SHIPPED + f"  {DEVIN}: floor=🔴\n", id="missing-parser"),
        pytest.param(SHIPPED + f"  {DEVIN}: parser=guesswork\n", id="unregistered-parser"),
        pytest.param(SHIPPED + "  <b>evil</b>: parser=codex-badge\n", id="not-a-login"),
        # A HUMAN account can never carry `[bot]`; naming one could let a session make
        # its own reviews satisfy merge freshness.
        pytest.param(
            f"reviewers:\n  {CODEX}: parser=codex-badge\n  octocat: primary parser=codex-badge\n",
            id="human-login",
        ),
        pytest.param(
            SHIPPED + "  CodeRabbitAI[bot]: parser=coderabbit-header\n", id="uppercase-login"
        ),
        # There is no `disabled` flag (a reviewer is removed by deleting its line), so
        # the word is malformed like any unknown flag, never a silent removal.
        pytest.param(SHIPPED + f"  {DEVIN}: disabled\n", id="disabled-is-an-unknown-word"),
        pytest.param(
            SHIPPED + "  github-advanced-security[bot]: parser=codeql\n", id="codeql-not-surface-only"
        ),
        pytest.param(
            SHIPPED + f"  {RABBIT}: surface-only parser=coderabbit-header\n", id="surface-only-scored-parser"
        ),
        pytest.param(SHIPPED + "other:\n  x: y\n", id="second-block"),
        pytest.param(f"  {CODEX}: primary parser=codex-badge\n", id="entry-outside-block"),
        pytest.param(f"reviewers:\n  {CODEX}: parser=codex-badge\n", id="no-primary"),
        pytest.param(
            SHIPPED + f"  {DEVIN}: primary parser=devin-marker\n", id="two-primaries"
        ),
        pytest.param(
            f"reviewers:\n  {CODEX}: primary surface-only parser=codex-badge\n",
            id="surface-only-primary",
        ),
    ],
)
def test_malformed_config_is_unknown_never_a_partial_table(monkeypatch, shipped):
    """A typo must never silently drop a reviewer: the whole table is unknown,
    so every consumer treats review evidence as unreadable."""
    table, error = _table(monkeypatch, shipped=shipped)
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
    monkeypatch.setattr(rf, "_SHIPPED", tmp_path / "absent.yaml")
    assert rf.configured_reviewers() == (None, "reviewers_config_missing")
    monkeypatch.setattr(rf, "_SHIPPED", tmp_path)  # a directory: read_text raises
    assert rf.configured_reviewers() == (None, "reviewers_config_unreadable")
    assert rf.primary_reviewer_login() is None


def test_no_install_local_file_is_read(monkeypatch, tmp_path):
    """A session-writable file outside the repo must not be able to grant review
    trust, so nothing but the shipped file is read: an install-local file that would
    make Devin primary changes nothing."""
    local = tmp_path / ".genesis" / "config" / "reviewers.local.yaml"
    local.parent.mkdir(parents=True)
    local.write_text(
        f"reviewers:\n  {CODEX}: parser=codex-badge\n  {DEVIN}: primary parser=devin-marker\n"
    )
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("_TEST_REVIEWERS_YAML", raising=False)
    # No module-level path outside the repo is left to patch.
    assert not [v for v in vars(rf).values() if isinstance(v, Path) and v != rf._SHIPPED]
    table, error = rf.configured_reviewers()
    assert error is None, error
    assert list(table) == [CODEX, DEVIN, RABBIT, "github-advanced-security[bot]"]
    assert rf.primary_reviewer_login() == CODEX


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
    monkeypatch.setenv(
        "_TEST_REVIEWERS_YAML",
        f"reviewers:\n  {CODEX}: parser=codex-badge\n  {DEVIN}: primary parser=devin-marker\n",
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
    # A table without Devin: only CodeRabbit may stand in.
    monkeypatch.setenv(
        "_TEST_REVIEWERS_YAML", SHIPPED + f"  {RABBIT}: body-findings parser=coderabbit-header\n"
    )
    assert guard._substitute_reviewers_at_head("1", H) == ([RABBIT], [])
    # An unreadable table is "could not evaluate", never "no substitute found".
    monkeypatch.setenv("_TEST_REVIEWERS_YAML", "garbage")
    assert guard._substitute_reviewers_at_head("1", H) is None


def test_freshness_names_an_unreadable_table_instead_of_a_missing_review(guard, monkeypatch):
    """A typo in the table must not read as 'no Codex review found' and send the
    operator to re-request a review that cannot help."""
    monkeypatch.setenv("_TEST_GH_HEAD_SHA", H)
    _reviews(monkeypatch, {"login": CODEX, "commit_id": H, "state": "COMMENTED"})
    monkeypatch.setenv("_TEST_REVIEWERS_YAML", SHIPPED + f"  {DEVIN}: disabled\n")
    block, msg, head = guard._check_codex_reviewed_head("1")
    assert block is True and head is None
    assert "reviewer table is unreadable (reviewers_config_malformed)" in msg
    assert "config/reviewers.yaml" in msg


def test_the_moved_parsers_are_the_ones_the_gate_uses(guard):
    """One implementation: the guard's names ARE the shared module's objects."""
    assert guard._cr_severity is rf.cr_severity
    assert guard._devin_finding is rf.devin_finding
    assert guard._INLINE_P1_RE is rf.INLINE_P1_RE



def test_enforced_logins_group_the_table_by_parser(monkeypatch):
    """Every reviewer the table names has its findings read, with its parser; a
    reviewer the table adds is enforced like a shipped one."""
    shipped = SHIPPED + (
        f"  {DEVIN}: parser=devin-marker\n"
        "  acme-review[bot]: parser=devin-marker\n"
        f"  {RABBIT}: parser=coderabbit-header\n"
    )
    _table(monkeypatch, shipped=shipped)
    sets, error = rf.enforced_logins()
    assert error is None
    assert sets["devin-marker"] == {DEVIN, "acme-review[bot]"}
    assert sets["coderabbit-header"] == {RABBIT}
    assert sets["codex-badge"] == {CODEX}
    monkeypatch.setenv("_TEST_REVIEWERS_YAML", "garbage")
    assert rf.enforced_logins() == (None, "reviewers_config_malformed")


def test_a_byte_order_mark_is_tolerated_in_the_file(monkeypatch, tmp_path):
    shipped = tmp_path / "reviewers.yaml"
    shipped.write_text("\ufeff" + SHIPPED, encoding="utf-8")
    monkeypatch.delenv("_TEST_REVIEWERS_YAML", raising=False)
    monkeypatch.setattr(rf, "_SHIPPED", shipped)
    assert rf.primary_reviewer_login() == CODEX


def test_a_long_line_is_parsed_in_linear_time(monkeypatch):
    """The reader runs on a hook path; a pathological line must not backtrack."""
    import time

    line = f"  {DEVIN}: parser=devin-marker" + " " * 200_000 + "x\n"
    start = time.perf_counter()
    _table(monkeypatch, shipped=SHIPPED + line)
    assert time.perf_counter() - start < 2.0


def test_freshness_block_names_a_non_default_primary(guard, monkeypatch):
    """When the table moves the primary, the Codex remediation text is not the way
    out; the block says who the gate actually waits on."""
    monkeypatch.setenv("_TEST_GH_HEAD_SHA", H)
    monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", "")
    monkeypatch.setenv(
        "_TEST_REVIEWERS_YAML",
        f"reviewers:\n  {CODEX}: parser=codex-badge\n  {DEVIN}: primary parser=devin-marker\n",
    )
    block, msg, _ = guard._check_codex_reviewed_head("1")
    assert block is True
    assert f"makes {DEVIN} the primary reviewer" in msg


def test_freshness_block_on_the_shipped_table_has_no_note(guard, monkeypatch):
    monkeypatch.setenv("_TEST_GH_HEAD_SHA", H)
    monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", "")
    block, msg, _ = guard._check_codex_reviewed_head("1")
    assert block is True and "reviewer table" not in msg


def test_budget_messages_name_a_reviewer_table_error(guard):
    """A typo in the table makes the budget unknown; the approval text must say it
    is a config problem, not that GitHub's review history was unreadable."""
    import review_enforcement_commit as rec

    result = {"status": "unknown", "errors": ["reviewers_config_malformed"]}
    for text in (rec._commit_budget_reason(result), guard._review_budget_message("7", result, "o/r")):
        assert "reviewer table is unreadable (reviewers_config_malformed)" in text
        assert "config/reviewers.yaml" in text
