"""Devin report bodies that say nothing beyond the inline findings are not surfaced.

The merge gate surfaces every review BODY it cannot parse, so a reader learns a
reviewer spoke. MEASURED 2026-10-31: on the open PRs every such body was a Devin
report in one of three shapes, so the NOTE fired on half the queue. A Devin body
now goes quiet only when it is template and nothing else AND its "found N" count
equals the inline comments that review posted. The body is the only place Devin
states its count, and findings can go unposted ("3 flags not posted on this PR by
your GitHub settings", measured on a past PR), so any other body stays visible.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

_GUARD = Path(__file__).resolve().parents[2] / "scripts" / "hooks" / "git_push_guard.py"
DEVIN = "devin-ai-integration[bot]"
BADGE = (
    "<!-- devin-review-badge-begin -->\n"
    '<a href="https://app.devin.ai/review/example/repo/pull/7" target="_blank"><picture>'
    '<img src="https://static.devin.ai/assets/badge.svg" alt="Devin Review"></picture></a>\n'
    "<!-- devin-review-badge-end -->\n"
)
FOUND = "**Devin Review** found {n} potential issue{s}.\n\n" + BADGE
NO_ISSUES = (
    "## ✅ Devin Review: No Issues Found\n\n"
    "Devin Review analyzed this PR and found no bugs or issues to report.\n\n"
    + BADGE
    + "\n<!-- devin-review-summary -->\n"
)
SUPERSEDED = (
    "> [!NOTE]\n> **Newer findings are available below.** Devin Review posted a newer "
    "report on this PR, in addition to the findings presented here.\n\n"
)
OUT_OF_DATE = (
    "> **This report is out of date.** Scroll down for Devin Review's latest report on this PR.\n\n"
)
NOT_POSTED = (
    "**Devin Review** found 3 potential issues.\n\n"
    "3 flags not posted on this PR by your GitHub settings — view them in Devin "
    "Review. ([Configure](https://app.devin.ai/org/example/settings/review))\n"
)


def _found(n: int) -> str:
    return FOUND.format(n=n, s="" if n == 1 else "s")


@pytest.fixture(scope="module")
def guard():
    import importlib.util

    spec = importlib.util.spec_from_file_location("git_push_guard", _GUARD)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestThePredicate:
    @pytest.mark.parametrize(
        ("body", "posted"),
        [
            (_found(3), 3),
            (_found(1), 1),
            (NO_ISSUES, 0),
            (SUPERSEDED + _found(2), 2),
            (OUT_OF_DATE + _found(2), 2),
            (_found(2).replace("found 2", "found 2 new"), 2),
        ],
    )
    def test_a_template_whose_count_was_posted_is_settled(self, guard, body, posted):
        assert guard._is_settled_devin_report(body, posted)

    @pytest.mark.parametrize(("body", "posted"), [(_found(3), 2), (_found(1), 0), (NO_ISSUES, 1)])
    def test_a_count_the_posted_comments_do_not_match_stays_visible(self, guard, body, posted):
        assert not guard._is_settled_devin_report(body, posted)

    def test_the_old_marker_beside_flags_not_posted_stays_visible(self, guard):
        body = (
            OUT_OF_DATE.rstrip("\n")
            + " 1 flag not posted on this PR by your GitHub settings.\n\n"
            + _found(2)
        )
        assert not guard._is_settled_devin_report(body, 2)

    def test_flags_not_posted_always_stay_visible(self, guard):
        """The measured shape: the sentence is not template, so it survives."""
        for posted in (0, 3):
            assert not guard._is_settled_devin_report(NOT_POSTED, posted)

    @pytest.mark.parametrize(
        "body",
        [
            _found(2) + "\nAlso: the migration drops a column without a backfill.",
            _found(2) + _found(1),
            NO_ISSUES + _found(1),
            "Looks good to me.",
            "",
        ],
    )
    def test_anything_beyond_one_template_stays_visible(self, guard, body):
        assert not guard._is_settled_devin_report(body, 2)

    # A fix review measured each of these going quiet on the first version.
    @pytest.mark.parametrize(
        "extra",
        [
            "\nDo not merge: rm -rf bug",  # a short leftover (under the old 24-char allowance)
            '\n<a href="x">P1: credentials leaked in config.yaml; rotate them</a>',  # link text
            '\n<a name="top">\nP1: the migration drops a table.\n',  # an unclosed link
        ],
    )
    def test_text_beside_a_found_zero_report_stays_visible(self, guard, extra):
        assert not guard._is_settled_devin_report(_found(0) + extra, 0)

    def test_a_body_over_the_cap_is_never_quiet_and_never_scanned(self, guard):
        """Bounded before any regex runs: the template regexes are super-linear on
        crafted text, and this runs on a hook path."""
        import time

        body = _found(0) + "<!--" * 16_000
        start = time.monotonic()
        assert not guard._is_settled_devin_report(body, 0)
        assert time.monotonic() - start < 0.05
        assert len(_found(3)) <= guard._DEVIN_REPORT_MAX_CHARS


def _report(guard, monkeypatch, reviews, inline):
    monkeypatch.setenv("_TEST_GH_PR_REVIEW_BODIES", "\n".join(json.dumps(r) for r in reviews))
    out = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="\n".join(json.dumps(c) for c in inline), stderr=""
    )
    uncounted: list = []
    with patch.object(guard.subprocess, "run", return_value=out):
        guard._check_inline_review_findings("100", uncounted_out=uncounted)
    return uncounted[0]["approx"]["unclassified_review_bodies"]


def _review(rid, body, login=DEVIN):
    return {"id": rid, "login": login, "body": body, "state": "COMMENTED"}


def _posted(cid, rid, login=DEVIN):
    return {
        "id": cid,
        "reply_to": None,
        "login": login,
        "type": "Bot",
        "assoc": "NONE",
        "body": "x",
        "review_id": rid,
    }


class TestTheBadgeStripsOnlyTheBadge:
    """Round 1 on #3810 (CodeRabbit, GLM, class audit): the badge strip once ran
    from marker to marker, so text Devin placed between the markers was deleted and
    a matching count quieted the body. Only the badge itself may go."""

    def test_text_between_the_markers_stays_visible(self, guard):
        body = _found(1).replace("</a>\n", "</a>\n> 3 flags not posted on this PR\n")
        assert not guard._is_settled_devin_report(body, 1)

    def test_text_after_an_unbalanced_begin_marker_stays_visible(self, guard):
        body = _found(1) + "<!-- devin-review-badge-begin -->\n2 flags not posted\n" + BADGE
        assert not guard._is_settled_devin_report(body, 1)

    def test_text_in_the_image_alt_stays_visible(self, guard):
        body = _found(1).replace('alt="Devin Review"', 'alt="3 flags not posted"')
        assert not guard._is_settled_devin_report(body, 1)

    def test_the_live_badge_with_its_dark_source_and_line_breaks_is_stripped(self, guard):
        """Both badge layouts seen live: one line with a dark-mode source, and
        the older indented one with a line per tag."""
        one_line = (
            "<!-- devin-review-badge-begin -->\n"
            '<a href="https://app.devin.ai/review/o/r/pull/7" target="_blank"><picture>'
            '<source media="(prefers-color-scheme: dark)" '
            'srcset="https://static.devin.ai/assets/gh-devin-review-dark.svg?v=4">'
            '<img src="https://static.devin.ai/assets/gh-devin-review-light.svg?v=4" '
            'alt="Devin Review"></picture></a>\n'
            "<!-- devin-review-badge-end -->\n"
        )
        indented = one_line.replace("><picture>", ">\n  <picture>\n    ").replace(
            "</picture></a>", "\n  </picture>\n</a>"
        )
        for badge in (one_line, indented):
            body = "**Devin Review** found 1 potential issue.\n\n" + badge
            assert guard._is_settled_devin_report(body, 1), badge


class TestInTheScan:
    def test_a_settled_report_is_not_surfaced(self, guard, monkeypatch, capsys):
        n = _report(guard, monkeypatch, [_review(9, _found(2))], [_posted(1, 9), _posted(2, 9)])
        assert n == 0
        assert "delivered in a review BODY" not in capsys.readouterr().err

    def test_a_report_short_of_its_posted_findings_is_surfaced(self, guard, monkeypatch, capsys):
        n = _report(guard, monkeypatch, [_review(9, _found(3))], [_posted(1, 9), _posted(2, 9)])
        assert n == 1
        assert f"[unclassified reviewer: {DEVIN}]" in capsys.readouterr().err

    def test_a_no_issues_report_with_nothing_posted_is_not_surfaced(
        self, guard, monkeypatch, capsys
    ):
        assert _report(guard, monkeypatch, [_review(9, NO_ISSUES)], []) == 0
        assert "delivered in a review BODY" not in capsys.readouterr().err

    def test_of_two_devin_reports_only_the_unsettled_one_is_surfaced(self, guard, monkeypatch):
        reviews = [_review(8, _found(1)), _review(9, _found(2))]
        assert _report(guard, monkeypatch, reviews, [_posted(1, 8), _posted(2, 9)]) == 1

    def test_comments_from_another_review_do_not_count(self, guard, monkeypatch):
        assert _report(guard, monkeypatch, [_review(9, _found(1))], [_posted(1, 8)]) == 1

    def test_a_reply_does_not_count_as_posted(self, guard, monkeypatch):
        reply = dict(_posted(2, 9), reply_to=1)
        assert _report(guard, monkeypatch, [_review(9, _found(2))], [_posted(1, 9), reply]) == 1

    def test_comments_missing_a_review_id_count_as_not_posted(self, guard, monkeypatch):
        """An older record or a degraded read carries no review id: the body stays."""
        c = _posted(1, None)
        assert _report(guard, monkeypatch, [_review(9, _found(1))], [c]) == 1

    def test_the_not_posted_report_is_surfaced(self, guard, monkeypatch, capsys):
        inline = [_posted(i, 9) for i in range(1, 4)]
        assert _report(guard, monkeypatch, [_review(9, NOT_POSTED)], inline) == 1

    @pytest.mark.parametrize("body", [_found(1), _found(0), NO_ISSUES])
    def test_only_a_devin_login_can_go_quiet(self, guard, monkeypatch, body):
        """Another author's body in Devin's shape stays visible, including a
        "found 0" one, which the posted-count check alone could not catch."""
        assert _report(guard, monkeypatch, [_review(9, body, "someone[bot]")], []) == 1

    def test_the_reads_ask_for_the_ids_the_count_needs(self, guard, monkeypatch):
        monkeypatch.delenv("_TEST_GH_PR_REVIEW_BODIES", raising=False)
        out = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        with patch.object(guard.subprocess, "run", return_value=out) as run:
            guard._check_inline_review_findings("100")
        jq = " ".join(
            tok for call in run.call_args_list for tok in call.args[0] if isinstance(tok, str)
        )
        assert "id: .id, login: .user.login" in jq
        assert "review_id: .pull_request_review_id" in jq
