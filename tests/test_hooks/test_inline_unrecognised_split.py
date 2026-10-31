"""The unrecognised inline-comment bucket, printed in three parts (issue #2417).

Before this split, a CodeQL security finding, a known reviewer's comment in a
format no parser reads, and a comment from an author the gate does not know all
shared one NOTE bounded to 5 lines. All three stay surfaced, never scored and
never blocking; what changes is that each kind is named for what it asks of the
reader, and the JSON count still totals all three.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

_GUARD = Path(__file__).resolve().parents[2] / "scripts" / "hooks" / "git_push_guard.py"

CODEQL = "github-advanced-security[bot]"
CODEX = "chatgpt-codex-connector[bot]"
P3 = (
    "**<sub><sub>![P3 Badge](https://img.shields.io/badge/P3-lightgrey)"
    "</sub></sub>  Keep the pattern inside its word**\n\nDetails."
)


@pytest.fixture(scope="module")
def guard():
    import importlib.util

    spec = importlib.util.spec_from_file_location("git_push_guard", _GUARD)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _c(cid, login, body, *, utype="Bot", reply_to=None, assoc="NONE"):
    return {
        "id": cid,
        "reply_to": reply_to,
        "login": login,
        "type": utype,
        "body": body,
        "assoc": assoc,
    }


def _scan(guard, comments):
    out = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="\n".join(json.dumps(c) for c in comments), stderr=""
    )
    with patch.object(guard.subprocess, "run", return_value=out):
        return guard._check_inline_review_findings("100")


def _codeql(cid, rule="Overly permissive regular expression range"):
    return _c(cid, CODEQL, f"## CodeQL / {rule}\n\nShow more details")


def test_a_codeql_finding_gets_its_own_note_with_a_remedy(guard, capsys):
    block, _ = _scan(guard, [_codeql(1)])
    assert not block, "CodeQL is surfaced, never scored"
    err = capsys.readouterr().err
    assert "1 inline CodeQL security finding(s) that no maintainer has answered" in err
    assert "fix it, or dismiss the alert with a reason" in err
    assert "  [CodeQL] ## CodeQL / Overly permissive regular expression range" in err
    assert "unrecognised" not in err, "a security finding must not read as format drift"


def test_a_known_reviewer_in_an_unparsed_format_is_called_format_drift(guard, capsys):
    block, _ = _scan(guard, [_c(1, CODEX, P3)])
    assert not block
    err = capsys.readouterr().err
    assert "from a KNOWN reviewer in a format this gate does not parse" in err
    assert f"  [unrecognised format: {CODEX}] " in err
    assert "CodeQL" not in err and "does not know" not in err


def test_an_unknown_author_is_called_unknown(guard, capsys):
    block, _ = _scan(guard, [_c(1, "some-new-reviewer[bot]", "Consider renaming this.")])
    assert not block
    err = capsys.readouterr().err
    assert "from an author this gate does not know" in err
    assert "  [unrecognised reviewer: some-new-reviewer[bot]] Consider renaming this." in err


def test_a_human_comment_is_an_unknown_author_never_a_score(guard, capsys):
    p1 = P3.replace("P3 Badge", "P1 Badge").replace("P3-lightgrey", "P1-red")
    block, _ = _scan(guard, [_c(1, "a-collaborator", p1, utype="User")])
    assert not block, "a non-Bot author's badge is never believed"
    assert "[unrecognised reviewer: a-collaborator]" in capsys.readouterr().err


def test_all_three_kinds_print_together_and_the_json_count_totals_them(guard, capsys):
    rows = [_codeql(1), _c(2, CODEX, P3), _c(3, "some-new-reviewer[bot]", "Hm.")]
    _scan(guard, rows)
    err = capsys.readouterr().err
    assert "1 inline CodeQL security finding(s)" in err
    assert "1 inline comment(s) from a KNOWN reviewer" in err
    assert "1 inline review comment(s) from an author this gate does not know" in err
    uncounted = []
    out = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="\n".join(json.dumps(c) for c in rows), stderr=""
    )
    with patch.object(guard.subprocess, "run", return_value=out):
        guard._check_inline_review_findings("100", uncounted_out=uncounted)
    assert uncounted and uncounted[0]["approx"]["unrecognised_reviewer"] == 3


def test_a_codeql_finding_a_maintainer_answered_is_not_listed(guard, capsys):
    reply = _c(
        2, "a-maintainer", "Dismissed: test fixture.", utype="User", reply_to=1, assoc="OWNER"
    )
    _scan(guard, [_codeql(1), reply])
    assert "CodeQL security finding" not in capsys.readouterr().err


def test_a_reply_from_a_non_maintainer_does_not_hide_it(guard, capsys):
    reply = _c(2, "drive-by", "Looks fine to me.", utype="User", reply_to=1, assoc="NONE")
    _scan(guard, [_codeql(1), reply])
    assert "1 inline CodeQL security finding(s)" in capsys.readouterr().err


def test_long_listings_are_bounded_with_the_rest_stated(guard, capsys):
    rows = [_codeql(i, rule=f"Rule {i}") for i in range(1, 14)]
    _scan(guard, rows)
    err = capsys.readouterr().err
    assert "13 inline CodeQL security finding(s)" in err
    assert err.count("  [CodeQL] ") == guard._UNRECOGNISED_LISTED
    assert "… and 3 more (listing bounded to 10; the count above is the total)" in err


@pytest.mark.parametrize(
    ("login", "body", "tag"),
    [
        (CODEX, P3, "unrecognised format"),
        ("some-new-reviewer[bot]", "Hm.", "unrecognised reviewer"),
    ],
)
def test_every_listing_is_bounded_with_the_rest_stated(guard, capsys, login, body, tag):
    _scan(guard, [_c(i, login, body) for i in range(1, 14)])
    err = capsys.readouterr().err
    assert err.count(f"  [{tag}: ") == guard._UNRECOGNISED_LISTED
    assert "… and 3 more (listing bounded to 10; the count above is the total)" in err


def test_without_a_codeql_entry_its_comments_read_as_an_unknown_author(guard, capsys, monkeypatch):
    real = guard._enforced_logins_or_block

    def _no_codeql(*a, **k):
        sets, block = real(*a, **k)
        return {k2: v for k2, v in sets.items() if k2 != "codeql"}, block

    monkeypatch.setattr(guard, "_enforced_logins_or_block", _no_codeql)
    _scan(guard, [_codeql(1)])
    err = capsys.readouterr().err
    assert "CodeQL security finding" not in err
    assert f"[unrecognised reviewer: {CODEQL}]" in err


def test_a_badge_in_a_codeql_body_is_never_scored(guard, capsys):
    """The registry calls CodeQL surface-only: nothing scores its findings. Its
    body was still searched for a P1 badge, so one quoted there would block."""
    p1 = P3.replace("P3 Badge", "P1 Badge").replace("P3-lightgrey", "P1-red")
    block, _ = _scan(guard, [_c(1, CODEQL, "## CodeQL / Some rule\n\n" + p1)])
    assert not block
    assert "1 inline CodeQL security finding(s)" in capsys.readouterr().err
