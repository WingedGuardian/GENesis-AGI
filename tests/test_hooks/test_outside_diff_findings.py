"""The CodeRabbit delivery channel the merge gate could not see.

When a finding's anchor line falls outside the PR's diff HUNKS, CodeRabbit
cannot create an inline review comment for it and puts the finding in the
review BODY instead, under an "Outside diff range comments" section. Neither
existing scan read it: the inline scan reads ``pulls/N/comments`` (a different
endpoint) and the review-body scan gates on ``_REVIEW_BOTS``, which does not
contain CodeRabbit.

MEASURED 2026-09-07 across all 84 then-open non-draft PRs: 27 deduped findings
on 23 PRs — 15 Major, 12 Minor, 0 Critical — every one invisible, including a
silent-write-loss Major (#1806) and a privacy Major (#1820) on PRs whose
``--check-pr`` printed ``inline-findings: ok``.

The fixtures below are the REAL body shapes, both of which appear in live data:
bare, and ``> ``-blockquoted when the section is nested inside an outer
``<details>``. #1834 carried the blockquoted form in its SECOND review and none
in its first, which is why every review is read rather than only the newest.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "hooks"
_GUARD = _SCRIPTS / "git_push_guard.py"


@pytest.fixture
def guard_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location("git_push_guard", _GUARD)
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("git_push_guard", mod)
    spec.loader.exec_module(mod)
    return mod


def _section(entries: str, *, quoted: bool = False, name: str = "Outside diff range") -> str:
    """A review body carrying one section, in the shape CodeRabbit really emits."""
    body = (
        f"<details>\n<summary>⚠️ {name} comments ({entries.count('`:')})"
        f"</summary><blockquote>\n\n{entries}\n\n</blockquote></details>"
    )
    if quoted:
        body = "\n".join("> " + line for line in body.split("\n"))
    return body


def _entry(path: str, lines: str, severity: str, title: str) -> str:
    """One file header plus one finding, matching the live layout."""
    return (
        f"<details>\n<summary>{path} (1)</summary><blockquote>\n\n"
        f"`{lines}`: _🗄️ Data Integrity_ | _{severity}_ | _🏗️ Heavy lift_\n\n"
        f"**{title}**\n\n</blockquote></details>"
    )


def _review(body: str, *, login: str = "coderabbitai[bot]", state: str = "COMMENTED") -> str:
    return json.dumps({"login": login, "body": body, "state": state})


@pytest.fixture(autouse=True)
def _in_diff(monkeypatch):
    """Every path these tests anchor on is IN the PR's diff.

    Without this the diff-scoping lane (#1728) diverts findings to the off-diff
    list, where a blocking assertion would fail for a reason unrelated to its
    name and a not-blocking assertion would pass vacuously."""
    monkeypatch.setenv(
        "_TEST_GH_PR_FILES",
        "\n".join(
            json.dumps({"filename": p, "previous_filename": None})
            for p in ("src/genesis/memory/store.py", "src/a.py", "src/b.py", "CHANGELOG.md")
        ),
    )


@pytest.fixture
def no_inline(guard_module):
    """No INLINE comments, so each test grades the review-body channel alone."""
    return patch.object(
        guard_module.subprocess,
        "run",
        return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
    )


class TestSeverityParsing:
    """The severity header here is INLINE (`… | _🟠 Major_ | …`), not a whole
    anchored field, so `_cr_severity`'s matcher cannot read it."""

    @pytest.mark.parametrize(
        ("token", "expected"),
        [
            ("🔴 Critical", "critical"),
            ("🟠 Major", "major"),
            ("🟡 Minor", "minor"),
            ("Trivial", "trivial"),
            ("Info", "info"),
        ],
    )
    def test_every_documented_level_is_read(self, guard_module, token, expected):
        # The reader is `_cr_severity`'s own field discipline applied inline:
        # split on `|`, complete italic fields only, severity = the field's
        # LAST word. All 27 live findings carry exactly this `_emoji Level_`
        # field shape.
        level, seen = guard_module._cr_severity_inline(f"_cat_ | _{token}_ | _effort_")
        assert level == expected, f"{token!r} must read as {expected!r}, got {level!r}"
        assert seen is True

    def test_unknown_level_is_none_not_guessed(self, guard_module):
        level, seen = guard_module._cr_severity_inline("_cat_ | _🟣 Catastrophic_ | _x_")
        assert level is None  # lands in the caller's canary, never given a weight
        assert seen is True

    def test_severity_word_inside_a_longer_word_does_not_match(self, guard_module):
        # `Majority` must not read as `major` — the field's LAST word must BE a
        # severity, so a substring cannot borrow one.
        level, _ = guard_module._cr_severity_inline("_cat_ | _Majority report_ | _x_")
        assert level is None

    def test_two_distinct_severity_fields_are_ambiguous(self, guard_module):
        # VERIFY-RED anchor (Codex P2, this PR): the first-match reader this
        # replaces returned "critical" here — a false block — because it took
        # the first severity-looking span body-wide instead of adjudicating
        # fields the way `_cr_severity` does. Two DISTINCT levels are a format
        # this code cannot adjudicate: unknown, canary, never a guess.
        level, seen = guard_module._cr_severity_inline("_Business Critical_ | _🟡 Minor_")
        assert level is None
        assert seen is True

    def test_unanimous_duplicate_severity_is_honoured(self, guard_module):
        # Mirrors `_cr_severity`'s unanimous-duplicate rule: demoting a real
        # Critical to the canary because a category field AGREES with it would
        # be the worse read.
        level, _ = guard_module._cr_severity_inline("_Business Critical_ | _🔴 Critical_")
        assert level == "critical"

    def test_a_non_field_chunk_is_skipped_not_invalidating(self, guard_module):
        # Deliberate divergence from the anchored whole-line parser: an inline
        # header can carry trailing prose between pipes; requiring all-italic
        # would push every such finding into the canary (fail-open for a gate).
        level, _ = guard_module._cr_severity_inline("see the note | _🟠 Major_")
        assert level == "major"


class TestSectionParsing:
    def test_bare_section_yields_the_finding(self, guard_module):
        body = _section(_entry("src/a.py", "10-12", "🟠 Major", "Do not do that"))
        assert guard_module._cr_outside_diff_entries(body)[0] == [
            ("src/a.py", "10-12", "major", "Do not do that")
        ]

    def test_blockquoted_section_yields_the_finding(self, guard_module):
        # The live #1834 shape. A parser anchoring `<summary>` to line-start
        # silently returns nothing here — and nothing is indistinguishable from
        # a clean PR, which is the whole defect class this file exists for.
        body = _section(_entry("src/a.py", "10-12", "🟠 Major", "Do not do that"), quoted=True)
        assert guard_module._cr_outside_diff_entries(body)[0] == [
            ("src/a.py", "10-12", "major", "Do not do that")
        ]

    @pytest.mark.parametrize("other", ["Nitpick", "Duplicate", "Additional"])
    def test_other_sections_are_not_collected(self, guard_module, other):
        body = _section(
            _entry("src/a.py", "10-12", "🟠 Major", "Not an outside-diff finding"),
            name=other,
        )
        assert guard_module._cr_outside_diff_entries(body)[0] == []

    def test_a_following_section_ends_the_outside_one(self, guard_module):
        body = (
            _section(_entry("src/a.py", "1-2", "🟠 Major", "Real"))
            + "\n"
            + _section(
                _entry("src/b.py", "3-4", "🔴 Critical", "Nitpick, not ours"), name="Nitpick"
            )
        )
        entries, _declared, _ = guard_module._cr_outside_diff_entries(body)
        assert [e[0] for e in entries] == ["src/a.py"]

    def test_entry_without_a_file_header_is_dropped(self, guard_module):
        # A stray range-and-severity line with no file to attribute it to is not
        # a finding; inventing a path for it would misreport where the defect is.
        body = (
            "<details>\n<summary>⚠️ Outside diff range comments (1)</summary><blockquote>\n\n"
            "`10-12`: _cat_ | _🟠 Major_ | _x_\n\n**Orphan**\n\n</blockquote></details>"
        )
        assert guard_module._cr_outside_diff_entries(body)[0] == []

    def test_titleless_entry_does_not_steal_the_next_title(self, guard_module):
        body = _section(
            "<details>\n<summary>src/a.py (2)</summary><blockquote>\n\n"
            "`1-1`: _cat_ | _🟠 Major_ | _x_\n\n"
            "`2-2`: _cat_ | _🟠 Major_ | _x_\n\n**Second finding title**\n\n"
            "</blockquote></details>"
        )
        entries, _declared, _ = guard_module._cr_outside_diff_entries(body)
        titles = {lines: title for _p, lines, _s, title in entries}
        assert titles["2-2"] == "Second finding title"
        assert titles["1-1"] == "", "an untitled finding must stay untitled, not borrow"


class TestGateBehaviour:
    """Severity policy: only Critical scores. A Major is surfaced and does NOT
    block, because an undelivered finding has no comment thread — the
    maintainer-reply route that clears every other finding does not exist for
    it, so blocking would make it satisfiable only by fixing."""

    def _run(self, guard_module, monkeypatch, no_inline, reviews):
        monkeypatch.setenv("_TEST_GH_PR_REVIEW_BODIES", "\n".join(reviews))
        with no_inline:
            return guard_module._check_inline_review_findings("100")

    def test_outside_diff_critical_blocks(self, guard_module, monkeypatch, no_inline, capsys):
        review = _review(_section(_entry("src/a.py", "1-2", "🔴 Critical", "Data loss")))
        block, msg = self._run(guard_module, monkeypatch, no_inline, [review])
        assert block is True
        assert "Data loss" in msg
        assert "outside-diff Critical" in msg

    def test_a_lone_outside_diff_blocker_is_counted_and_actionable(
        self, guard_module, monkeypatch, no_inline
    ):
        # VERIFY-RED anchor (Codex P2, this PR): with an outside-diff Critical
        # as the ONLY blocker, the summary counted "0 …" of everything above a
        # blocking score, and the sole printed remedy was "reply in-thread" —
        # an impossible instruction, because this channel's findings have no
        # comment thread to reply in.
        review = _review(_section(_entry("src/a.py", "1-2", "🔴 Critical", "Data loss")))
        block, msg = self._run(guard_module, monkeypatch, no_inline, [review])
        assert block is True
        assert "1 outside-diff Critical" in msg
        assert "NO thread" in msg
        assert "dismissal of the carrying review" in msg

    def test_outside_diff_major_does_not_block_but_is_surfaced(
        self, guard_module, monkeypatch, no_inline, capsys
    ):
        review = _review(
            _section(
                _entry(
                    "src/genesis/memory/store.py",
                    "169-174",
                    "🟠 Major",
                    "Do not deduplicate scoped preferences by content alone.",
                )
            )
        )
        block, _ = self._run(guard_module, monkeypatch, no_inline, [review])
        assert block is False
        err = capsys.readouterr().err
        # Both halves matter: not blocking is only correct if it is also VISIBLE.
        assert "outside-diff Major" in err
        assert "Do not deduplicate scoped preferences by content alone." in err
        assert "src/genesis/memory/store.py:169-174" in err

    def test_outside_diff_minor_is_surfaced_and_does_not_block(
        self, guard_module, monkeypatch, no_inline, capsys
    ):
        review = _review(_section(_entry("src/a.py", "1-2", "🟡 Minor", "Small thing")))
        block, _ = self._run(guard_module, monkeypatch, no_inline, [review])
        assert block is False
        assert "outside-diff Minor" in capsys.readouterr().err

    def test_unknown_severity_lands_in_the_canary(
        self, guard_module, monkeypatch, no_inline, capsys
    ):
        review = _review(_section(_entry("src/a.py", "1-2", "🟣 Catastrophic", "Odd")))
        block, _ = self._run(guard_module, monkeypatch, no_inline, [review])
        assert block is False
        assert "could not read" in capsys.readouterr().err

    def test_findings_are_deduped_across_re_reviews(
        self, guard_module, monkeypatch, no_inline, capsys
    ):
        # CodeRabbit restates an undelivered finding on EVERY re-review, so an
        # undeduped count inflates with each push.
        entry = _entry("src/a.py", "1-2", "🟠 Major", "Same finding")
        reviews = [_review(_section(entry)) for _ in range(3)]
        block, _ = self._run(guard_module, monkeypatch, no_inline, reviews)
        assert block is False
        err = capsys.readouterr().err
        assert err.count("[outside-diff Major]") == 1
        assert "1 CodeRabbit finding(s) delivered in the REVIEW BODY" in err

    def test_a_finding_present_in_only_the_second_review_is_found(
        self, guard_module, monkeypatch, no_inline, capsys
    ):
        # The live #1834 case: review 1 has no outside-diff section at all.
        reviews = [
            _review("<details><summary>🧹 Nitpick comments (1)</summary></details>"),
            _review(_section(_entry("src/b.py", "9-9", "🟠 Major", "Only in review two"))),
        ]
        block, _ = self._run(guard_module, monkeypatch, no_inline, reviews)
        assert block is False
        assert "Only in review two" in capsys.readouterr().err

    def test_dismissed_review_findings_are_excluded(
        self, guard_module, monkeypatch, no_inline, capsys
    ):
        # A maintainer dismissing the review IS the engagement — the one
        # disposition route that does exist for a threadless finding.
        review = _review(
            _section(_entry("src/a.py", "1-2", "🔴 Critical", "Dismissed")), state="DISMISSED"
        )
        block, _ = self._run(guard_module, monkeypatch, no_inline, [review])
        assert block is False
        assert "Dismissed" not in capsys.readouterr().err

    def test_non_coderabbit_review_bodies_are_ignored(
        self, guard_module, monkeypatch, no_inline, capsys
    ):
        review = _review(
            _section(_entry("src/a.py", "1-2", "🔴 Critical", "Not from CodeRabbit")),
            login="some-human",
        )
        block, _ = self._run(guard_module, monkeypatch, no_inline, [review])
        assert block is False
        # `block is False` alone is ALSO what a parser that read nothing
        # produces, so on its own this test stays green if the login filter is
        # deleted and the body simply fails to parse — it would pass for a
        # reason unrelated to its name. The absence assertion is what makes it
        # discriminating: the finding must be absent from the SURFACED output
        # too, not merely unscored. (Matching `test_dismissed_review_findings_
        # are_excluded` above.)
        err = capsys.readouterr().err
        assert "Not from CodeRabbit" not in err
        assert "outside-diff" not in err

    def test_critical_on_a_doc_path_does_not_block(
        self, guard_module, monkeypatch, no_inline, capsys
    ):
        # Only the FLOOR consults the exclusions, and it reports through the
        # SAME doc list the inline path uses so a reader sees one story.
        review = _review(_section(_entry("CHANGELOG.md", "1-2", "🔴 Critical", "Prose")))
        block, _ = self._run(guard_module, monkeypatch, no_inline, [review])
        assert block is False
        assert "doc CodeRabbit Critical/Major" in capsys.readouterr().err

    def test_critical_outside_the_diff_is_not_scored(
        self, guard_module, monkeypatch, no_inline, capsys, offdiff_lock
    ):
        # #1728: a base-branch finding is not this PR's to answer. Off-diff
        # routing is this test's SUBJECT, so it declares itself to the
        # conftest lock (see `offdiff_lock`) — without the declaration the
        # lock fails the test for silently discounting a finding.
        offdiff_lock.expected()
        review = _review(_section(_entry("src/not_in_diff.py", "1-2", "🔴 Critical", "Base")))
        block, _ = self._run(guard_module, monkeypatch, no_inline, [review])
        assert block is False
        assert "off-diff" in capsys.readouterr().err


class TestQuotedContent:
    """A review body is a code-bearing document — it quotes diffs and embeds
    ```suggestion blocks. Parsing its raw lines fails in BOTH directions, and
    the fail-OPEN one does not announce itself."""

    def test_a_fenced_summary_does_not_reassign_the_current_file(
        self, guard_module, monkeypatch, no_inline
    ):
        # FAIL-OPEN, measured before the fence mask: the quoted <summary> below
        # reassigned `current_file`, so the REAL Critical two lines later was
        # emitted against docs/unrelated.md, routed to the doc-skipped lane, and
        # did NOT block. The finding was read, then attributed to the wrong file.
        body = _section(
            "<details>\n<summary>src/a.py (1)</summary><blockquote>\n\n"
            "```md\n<summary>docs/unrelated.md (1)</summary>\n```\n\n"
            "`10-12`: _cat_ | _🔴 Critical_ | _e_\n\n**Real critical**\n\n"
            "</blockquote></details>"
        )
        entries, _, _ = guard_module._cr_outside_diff_entries(body)
        assert entries == [("src/a.py", "10-12", "critical", "Real critical")]
        monkeypatch.setenv("_TEST_GH_PR_REVIEW_BODIES", _review(body))
        with no_inline:
            block, _msg = guard_module._check_inline_review_findings("100")
        assert block is True, "a real Critical must not be misattributed onto a doc path"

    def test_a_backticked_tag_in_prose_does_not_move_depth(self, guard_module):
        # VERIFY-RED anchor (Codex P1, this PR): `str.count` saw the quoted
        # `</details>` below as a real closer, so every later depth read one
        # level low, the next genuine file header failed its depth test,
        # `current_file` kept docs/a.md, and the Critical on src/a.py was
        # misattributed to a doc path — with declared == parsed, so the
        # shortfall canary stayed quiet. A renderer treats inline code as
        # text; the depth reader must too.
        first = (
            "<details>\n<summary>docs/a.md (1)</summary><blockquote>\n\n"
            "`1-2`: _cat_ | _🟡 Minor_ | _e_\n\n**First**\n\n"
            "Close the block with `</details>` when done.\n\n"
            "</blockquote></details>"
        )
        second = _entry("src/a.py", "3-4", "🔴 Critical", "Real critical")
        entries, _, _ = guard_module._cr_outside_diff_entries(_section(first + "\n\n" + second))
        assert entries == [
            ("docs/a.md", "1-2", "minor", "First"),
            ("src/a.py", "3-4", "critical", "Real critical"),
        ]

    def test_an_entry_inside_a_suggestion_block_is_not_a_finding(self, guard_module):
        # The PHANTOM direction — the #1677 defect, which this file's own
        # `_cr_markup_mask` exists to prevent. Blocking a merge on quoted code
        # is loud rather than silent, but it is still wrong.
        body = _section(
            "<details>\n<summary>src/a.py (1)</summary><blockquote>\n\n"
            "`1-2`: _cat_ | _🟡 Minor_ | _e_\n\n**Real minor**\n\n"
            "```suggestion\n`99-99`: _cat_ | _🔴 Critical_ | _e_\n```\n\n"
            "</blockquote></details>"
        )
        entries, _, _ = guard_module._cr_outside_diff_entries(body)
        assert entries == [("src/a.py", "1-2", "minor", "Real minor")]

    def test_the_title_search_is_bounded_in_content_lines(self, guard_module):
        # The break on the next entry/file header bounds the search in the
        # NORMAL layout; the budget is what bounds it when neither follows —
        # a lone finding trailed by prose. Without the budget the search runs
        # to end-of-section and adopts an unrelated bold line as the title,
        # naming the wrong thing at the right location.
        body = _section(
            "<details>\n<summary>src/a.py (1)</summary><blockquote>\n\n"
            "`1-2`: _cat_ | _🟠 Major_ | _e_\n\n"
            + "\n".join(f"filler line {i}" for i in range(8))
            + "\n\n**Unrelated bold far below**\n\n</blockquote></details>"
        )
        entries, _, _ = guard_module._cr_outside_diff_entries(body)
        assert entries[0][3] == "", "a distant bold line is not this finding's title"

    def test_a_quoted_bold_line_is_not_borrowed_as_a_title(self, guard_module):
        body = _section(
            "<details>\n<summary>src/a.py (1)</summary><blockquote>\n\n"
            "`1-2`: _cat_ | _🟠 Major_ | _e_\n\n"
            "```md\n**Quoted heading, not the title**\n```\n\n"
            "**The real title**\n\n</blockquote></details>"
        )
        entries, _, _ = guard_module._cr_outside_diff_entries(body)
        assert entries[0][3] == "The real title"


class TestStructuralAttribution:
    """A `<summary>` is honoured only where the DOCUMENT puts it, never because
    its text appears somewhere. Both roles were forgeable from content a PR
    author controls — their own source, quoted back by the reviewer."""

    def test_entry_shaped_prose_mid_line_is_not_a_finding(self, guard_module):
        # VERIFY-RED anchor (Codex P2, this PR): the entry regex was searched
        # anywhere in the line, so prose QUOTING an entry shape mid-sentence
        # ("contains `99`: …") parsed as a second finding — a phantom Critical
        # blocking on text nobody wrote. Entries are emitted at line start; the
        # anchor plus the surplus reconciliation below hold that margin.
        body = (
            "<details>\n<summary>⚠️ Outside diff range comments (1)"
            "</summary><blockquote>\n\n"
            "<details>\n<summary>src/a.py (1)</summary><blockquote>\n\n"
            "`1-2`: _cat_ | _🟡 Minor_ | _e_\n\n**Real**\n\n"
            "The source contains `99`: _cat_ | _🔴 Critical_ | _e_ in a string.\n\n"
            "</blockquote></details>\n\n</blockquote></details>"
        )
        entries, _, _ = guard_module._cr_outside_diff_entries(body)
        assert entries == [("src/a.py", "1-2", "minor", "Real")]

    def test_unfenced_summary_in_prose_does_not_reassign_the_file(self, guard_module):
        # The one the fenced-content fix did NOT close, and the nastiest of the
        # set: the finding is still COUNTED, so declared == parsed and the
        # shortfall canary stays quiet while a real Critical is filed against a
        # doc path and silently doc-skipped.
        body = (
            "<details>\n<summary>Outside diff range comments (1)</summary><blockquote>\n"
            "<details>\n<summary>src/real.py (1)</summary><blockquote>\n\n"
            "Prose that happens to quote: <summary>CHANGELOG.md (1)</summary>\n\n"
            "`50-60`: _c_ | _🔴 Critical_ | _e_\n\n**Real critical**\n\n"
            "</blockquote></details>\n</blockquote></details>"
        )
        entries, declared, _ = guard_module._cr_outside_diff_entries(body)
        assert entries == [("src/real.py", "50-60", "critical", "Real critical")]
        assert declared == 1

    def test_a_summary_needs_a_details_opener_immediately_before_it(self, guard_module):
        # HTML requires <summary> to be the first child of its <details>. That
        # adjacency is the invariant the generator satisfies and an injection
        # cannot — depth alone does not separate them, because injected prose
        # sits at the SAME depth as the block it was injected into.
        lines = [
            "<details>",
            "<summary>a/b.py (1)</summary>",
            "prose <summary>c/d.py (1)</summary>",
        ]
        structural = guard_module._cr_summary_structural(lines, [False, False, False])
        assert structural == [False, True, False]

    def test_an_unfenced_section_header_in_prose_does_not_truncate(self, guard_module):
        # Isolates the STRUCTURAL check on the section role. The fenced variant
        # below is caught by the mask and the deep variant by the depth gate, so
        # each layer needs a case where it is the ONLY thing standing — a
        # mutation that deletes one otherwise survives behind its sibling.
        body = (
            "<details>\n<summary>Outside diff range comments (1)</summary><blockquote>\n"
            "Intro prose quoting <summary>Nitpick comments (4)</summary> inline.\n"
            "<details>\n<summary>src/a.py (1)</summary><blockquote>\n\n"
            "`2-2`: _c_ | _🔴 Critical_ | _e_\n\n**Still ours**\n\n"
            "</blockquote></details>\n</blockquote></details>"
        )
        entries, _, _ = guard_module._cr_outside_diff_entries(body)
        assert entries == [("src/a.py", "2-2", "critical", "Still ours")]

    def test_a_file_header_nested_too_deep_is_not_honoured(self, guard_module):
        # Isolates the DEPTH gate on the file role. This tag IS structurally
        # adjacent to a <details> opener — so the adjacency check passes it —
        # and only its nesting level says it is not a file header. CodeRabbit
        # nests collapsible blocks inside findings, so a deeper <details>
        # carrying the file-header shape is a live document shape, not a
        # contrived one.
        body = (
            "<details>\n<summary>Outside diff range comments (1)</summary><blockquote>\n"
            "<details>\n<summary>src/a.py (1)</summary><blockquote>\n\n"
            "<details>\n<summary>docs/decoy.md (1)</summary><blockquote>\n"
            "</blockquote></details>\n\n"
            "`2-2`: _c_ | _🔴 Critical_ | _e_\n\n**Attributed to the real file**\n\n"
            "</blockquote></details>\n</blockquote></details>"
        )
        entries, _, _ = guard_module._cr_outside_diff_entries(body)
        assert entries == [("src/a.py", "2-2", "critical", "Attributed to the real file")]

    def test_a_fenced_section_header_does_not_truncate_the_section(self, guard_module):
        # Section boundaries used to be found over the RAW body, so a quoted
        # section name ended the real section early and dropped everything after.
        body = (
            "<details>\n<summary>Outside diff range comments (1)</summary><blockquote>\n"
            "<details>\n<summary>src/a.py (1)</summary><blockquote>\n\n"
            "```suggestion\n<summary>Nitpick comments (9)</summary>\n```\n\n"
            "`2-2`: _c_ | _🔴 Critical_ | _e_\n\n**Survives**\n\n"
            "</blockquote></details>\n</blockquote></details>"
        )
        entries, _, _ = guard_module._cr_outside_diff_entries(body)
        assert entries == [("src/a.py", "2-2", "critical", "Survives")]

    @pytest.mark.parametrize(
        ("name", "sep"),
        [("NEL", ""), ("LS", " "), ("PS", " ")],
    )
    def test_unicode_line_separators_do_not_swallow_an_entry(self, guard_module, name, sep):
        # str.split("\\n") does not break on these, so the entry landed on its
        # file header's line, the header branch consumed the line, and the
        # finding vanished. `gh --jq` emits NEL literally inside JSON strings —
        # which is why _fetch_comments_paged splits on "\\n" only — so it really
        # reaches this parser.
        body = (
            "<details>\n<summary>Outside diff range comments (1)</summary><blockquote>\n"
            "<details>\n<summary>src/a.py (1)</summary><blockquote>"
            + sep
            + sep
            + "`2-2`: _c_ | _🔴 Critical_ | _e_"
            + sep
            + sep
            + "**Found anyway**\n\n"
            "</blockquote></details>\n</blockquote></details>"
        )
        entries, _, _ = guard_module._cr_outside_diff_entries(body)
        assert entries == [("src/a.py", "2-2", "critical", "Found anyway")], f"{name} lost it"

    def test_blockquoted_bodies_are_parsed_and_masked(self, guard_module):
        # MEASURED: 23 of 23 live bodies carry blockquoted lines, and 74 real
        # fence lines went UNMASKED because the shared mask matches structure
        # with startswith on the stripped line, which a "> " prefix defeats.
        body = "\n".join(
            "> " + line
            for line in (
                "<details>",
                "<summary>Outside diff range comments (1)</summary><blockquote>",
                "<details>",
                "<summary>src/a.py (1)</summary><blockquote>",
                "",
                "```suggestion",
                "<summary>docs/decoy.md (1)</summary>",
                "```",
                "",
                "`2-2`: _c_ | _🔴 Critical_ | _e_",
                "",
                "**Blockquoted and safe**",
            )
        )
        entries, _, _ = guard_module._cr_outside_diff_entries(body)
        assert entries == [("src/a.py", "2-2", "critical", "Blockquoted and safe")]


class TestShortfallFailsClosed:
    """A body declaring more findings than were parsed is an INCOMPLETE read."""

    def test_a_balanced_fence_hiding_a_finding_blocks(self, guard_module, monkeypatch, no_inline):
        # The one suppression shape the fence mask cannot judge — a balanced
        # fence around a real finding is indistinguishable from legitimately
        # quoted content, so the mask correctly hides it and the COUNT is what
        # notices something went missing.
        body = (
            "<details>\n<summary>Outside diff range comments (2)</summary><blockquote>\n"
            + _entry("src/a.py", "1-1", "🟡 Minor", "Visible")
            + "\n```\n"
            + _entry("src/a.py", "2-2", "🔴 Critical", "Hidden")
            + "\n```\n</blockquote></details>"
        )
        monkeypatch.setenv("_TEST_GH_PR_REVIEW_BODIES", _review(body))
        with no_inline:
            block, msg = guard_module._check_inline_review_findings("100")
        assert block is True
        assert "declares 2 finding(s), parsed 1" in msg

    def test_more_parsed_than_declared_blocks_as_unreadable(
        self, guard_module, monkeypatch, no_inline
    ):
        # The OTHER direction of the reconciliation (Codex P2, this PR): a
        # surplus means prose was mis-read as a finding, and which entries are
        # the phantoms is unknowable from here — so the read blocks as
        # unreliable, an explained stop, rather than blocking ON a finding
        # nobody wrote. VERIFY-RED: before the check, the phantom simply
        # passed through as a real entry.
        body = (
            "<details>\n<summary>⚠️ Outside diff range comments (1)"
            "</summary><blockquote>\n"
            + _entry("src/a.py", "1-1", "🟡 Minor", "One")
            + "\n"
            + _entry("src/b.py", "2-2", "🟡 Minor", "Two")
            + "\n</blockquote></details>"
        )
        monkeypatch.setenv("_TEST_GH_PR_REVIEW_BODIES", _review(body))
        with no_inline:
            block, msg = guard_module._check_inline_review_findings("100")
        assert block is True
        assert "surplus entries indicate prose" in msg


class TestSeverityMerge:
    """One finding restated across reviews merges by MAX, never by assignment."""

    def _run(self, guard_module, monkeypatch, no_inline, severities):
        bodies = [
            _review(_section(_entry("src/a.py", "10-12", s, "Same finding"))) for s in severities
        ]
        monkeypatch.setenv("_TEST_GH_PR_REVIEW_BODIES", "\n".join(bodies))
        with no_inline:
            return guard_module._check_inline_review_findings("100")[0]

    @pytest.mark.parametrize(
        "order",
        [
            ["🔴 Critical", "🟡 Minor"],  # downgrade — the fail-OPEN order
            ["🟡 Minor", "🔴 Critical"],  # upgrade
            ["🔴 Critical", "🟣 Unreadable"],  # severity field drops out
        ],
        ids=["downgraded", "upgraded", "severity-lost"],
    )
    def test_a_critical_survives_being_restated(self, guard_module, monkeypatch, no_inline, order):
        # Reviews arrive oldest-first, so last-write-wins made the VERDICT
        # order-dependent: Critical-then-Minor stopped blocking while
        # Minor-then-Critical blocked. Measured, all three cells.
        assert self._run(guard_module, monkeypatch, no_inline, order) is True

    def test_the_channel_still_reports_the_highest_level_seen(
        self, guard_module, monkeypatch, no_inline, capsys
    ):
        self._run(guard_module, monkeypatch, no_inline, ["🔴 Critical", "🟡 Minor"])
        err = capsys.readouterr().err
        assert "outside-diff Critical" in err
        assert "outside-diff Minor" not in err


class TestDeclaredCountReconciliation:
    """The format states its own counts — the one closed-set fact available
    about a document nobody controls."""

    def test_a_shortfall_is_reported(self, guard_module, monkeypatch, no_inline, capsys):
        # Header claims 3; the section carries one parseable entry.
        body = (
            "<details>\n<summary>⚠️ Outside diff range comments (3)</summary><blockquote>\n\n"
            + _entry("src/a.py", "1-2", "🟠 Major", "Only one parsed")
            + "\n\n</blockquote></details>"
        )
        monkeypatch.setenv("_TEST_GH_PR_REVIEW_BODIES", _review(body))
        with no_inline:
            block, msg = guard_module._check_inline_review_findings("100")
        # BLOCKS, not merely notes. A body that declares more than was parsed is
        # an incomplete finding scan, and this gate treats an incomplete scan as
        # "not clean, retry" everywhere else — degrading it to an advisory here
        # would leave the detected under-parse fail-open.
        assert block is True
        assert "declares 3 finding(s), parsed 1" in msg

    def test_no_shortfall_reports_nothing(self, guard_module, monkeypatch, no_inline, capsys):
        body = _section(_entry("src/a.py", "1-2", "🟠 Major", "Parsed"))
        monkeypatch.setenv("_TEST_GH_PR_REVIEW_BODIES", _review(body))
        with no_inline:
            guard_module._check_inline_review_findings("100")
        assert "declares" not in capsys.readouterr().err


class TestFailDirection:
    """An unreadable second channel must block, exactly as the first one does —
    a scan that cannot be read is 'not clean, retry', never a silent pass."""

    def test_unreadable_review_fetch_blocks(self, guard_module, monkeypatch, no_inline):
        monkeypatch.setenv("_TEST_GH_PR_REVIEW_BODIES", "{not json")
        with no_inline:
            block, msg = guard_module._check_inline_review_findings("100")
        assert block is True
        assert "UNREADABLE" in msg
        # Assert WHICH failure, not merely that it blocked. `reviews is None`
        # always implies `complete is False`, so the incomplete-read check also
        # blocks this input — a bare `assert block` passes with the unreadable
        # branch deleted (mutation-measured: it survived). The message is the
        # maintainer's only signal for which of the two happened, so it is the
        # thing worth pinning.
        assert "outside-diff findings" in msg, (
            "must report the unreadable-fetch cause, not the incomplete-read one"
        )

    def test_report_survives_an_unreadable_review_fetch(self, guard_module, monkeypatch, capsys):
        """An unreadable second channel must not erase the FIRST channel's report.

        Returning early on the failed fetch would discard every list the inline
        loop had already built, before any of them is printed — one transient
        `gh` failure replacing the whole pre-merge report with a single line."""
        codex_p1 = {
            "id": 1,
            "reply_to": None,
            "login": "chatgpt-codex-connector[bot]",
            "type": "Bot",
            "path": "src/a.py",
            "body": "**<sub><sub>![P1 Badge](https://img.shields.io/badge/P1-red)"
            "</sub></sub>  Inline finding that must still be reported**\n\nDetails.",
        }
        monkeypatch.setenv("_TEST_GH_PR_REVIEW_BODIES", "{not json")
        with patch.object(
            guard_module.subprocess,
            "run",
            return_value=subprocess.CompletedProcess(
                args=[], returncode=0, stdout=json.dumps(codex_p1), stderr=""
            ),
        ):
            block, msg = guard_module._check_inline_review_findings("100")
        assert block is True
        # The P1's OWN message wins here, not "UNREADABLE": the score check runs
        # before the unreadable check precisely so a finding already READ blocks
        # with its own reason rather than being flattened into a fetch error.
        # Both block, so the fail direction is identical — what differs is how
        # much the operator is told, which is the whole point of this test.
        combined = capsys.readouterr().err + msg
        assert "Inline finding that must still be reported" in combined
        assert "review score" in msg

    def test_empty_review_set_is_clean_not_unreadable(self, guard_module, monkeypatch, no_inline):
        # A PR with no reviews is a legitimately clean read, distinct from a
        # failed one — conflating them would block every unreviewed PR.
        monkeypatch.setenv("_TEST_GH_PR_REVIEW_BODIES", "")
        with no_inline:
            block, _ = guard_module._check_inline_review_findings("100")
        assert block is False


class TestAuditResiduals:
    """Two second-order gaps the adversarial audit found IN the fixes above.

    Both were reproduced end-to-end before being fixed, and neither was
    fail-open — they made a BLOCKING message wrong, which is its own defect:
    a gate that names a finding nobody wrote trains the reflex override that
    issue #1642 warns about.
    """

    def test_a_code_span_broken_across_lines_does_not_move_depth(self, guard_module):
        # VERIFY-RED: with `[^`\n]` in the span pattern (the per-line mask),
        # a span containing a newline cannot be closed, so the quoted
        # `</details>` inside it stays live, depth reads one level low, and
        # the next file's Critical is misattributed to docs/a.md — the P1
        # shape again, one newline away, with declared == parsed so the
        # canary stays quiet. CommonMark terminates a span at a BLANK line,
        # not at a line end, which is why the mask is paragraph-scoped.
        first = (
            "<details>\n<summary>docs/a.md (1)</summary><blockquote>\n\n"
            "`1-2`: _cat_ | _🟡 Minor_ | _e_\n\n**First**\n\n"
            "Close with `</details>\nwhen` done.\n\n"
            "</blockquote></details>"
        )
        second = _entry("src/a.py", "3-4", "🔴 Critical", "Real critical")
        entries, _, _ = guard_module._cr_outside_diff_entries(_section(first + "\n\n" + second))
        assert ("src/a.py", "3-4", "critical", "Real critical") in entries, (
            "a Critical must not be misattributed to the doc path by a "
            "code span the mask failed to close"
        )

    def test_a_surplus_critical_reports_the_surplus_not_the_finding(
        self, guard_module, monkeypatch, no_inline
    ):
        # VERIFY-RED: without the quarantine `continue`, the suspect entries
        # still reached the score, the score branch returned FIRST, and the
        # message named the possibly-phantom Critical as a real finding while
        # the "surplus" explanation never surfaced. Both versions block; only
        # this one tells the truth about why.
        body = (
            "<details>\n<summary>⚠️ Outside diff range comments (1)"
            "</summary><blockquote>\n"
            + _entry("src/a.py", "1-1", "🔴 Critical", "PhantomOrReal")
            + "\n"
            + _entry("src/b.py", "2-2", "🟡 Minor", "Two")
            + "\n</blockquote></details>"
        )
        monkeypatch.setenv("_TEST_GH_PR_REVIEW_BODIES", _review(body))
        with no_inline:
            block, msg = guard_module._check_inline_review_findings("100")
        assert block is True
        assert "surplus entries indicate prose" in msg
        assert "PhantomOrReal" not in msg, (
            "a quarantined batch must not be named as a real finding"
        )
