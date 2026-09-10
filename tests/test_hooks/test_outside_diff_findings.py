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
        entries, _declared, _, _ = guard_module._cr_outside_diff_entries(body)
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
        entries, _declared, _, _ = guard_module._cr_outside_diff_entries(body)
        titles = {lines: title for _p, lines, _s, title in entries}
        assert titles["2-2"] == "Second finding title"
        assert titles["1-1"] == "", "an untitled finding must stay untitled, not borrow"


class TestGateBehaviour:
    """Severity policy: NOTHING in this channel scores. Every level is surfaced
    and none blocks.

    That is an owner decision taken at the escalation cap (2026-09-09), not a
    convenience. Four consecutive review rounds on this parser each produced a
    defect of one class — text from a third-party rendered document read as
    structure or identity — and every one was only dangerous because a mis-read
    could move a BLOCKING verdict. Removing the verdict makes the whole class
    unreachable instead of patched. Measured cost: nil. Across all 84 then-open
    non-draft PRs (2026-09-07) the channel carried 27 findings — 15 Major, 12
    Minor, ZERO Critical — so the scoring path never fired on live data, while
    surfacing keeps everything the channel was built to recover.
    """

    def _run(self, guard_module, monkeypatch, no_inline, reviews):
        monkeypatch.setenv("_TEST_GH_PR_REVIEW_BODIES", "\n".join(reviews))
        with no_inline:
            return guard_module._check_inline_review_findings("100")

    def test_outside_diff_critical_is_surfaced_and_does_not_block(
        self, guard_module, monkeypatch, no_inline, capsys
    ):
        """VERIFY-RED against the PREVIOUS design, which scored this at 1.0."""
        review = _review(_section(_entry("src/a.py", "1-2", "🔴 Critical", "Data loss")))
        block, _msg = self._run(guard_module, monkeypatch, no_inline, [review])
        assert block is False, "this channel is advisory; it must never block"
        err = capsys.readouterr().err
        assert "[outside-diff Critical] Data loss" in err, "and it must still be SEEN"
        assert "NEVER SCORED" in err

    def test_a_lone_outside_diff_finding_produces_no_block_message_at_all(
        self, guard_module, monkeypatch, no_inline
    ):
        """The impossible-remedy problem is DISSOLVED rather than reworded.

        An earlier round had to explain that these findings have no comment
        thread to reply in, because the block message told the operator to
        reply in one. A channel that never blocks never prints that message.
        """
        review = _review(_section(_entry("src/a.py", "1-2", "🔴 Critical", "Data loss")))
        block, msg = self._run(guard_module, monkeypatch, no_inline, [review])
        assert (block, msg) == (False, "")

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

    def test_critical_on_a_doc_path_is_surfaced_like_any_other(
        self, guard_module, monkeypatch, no_inline, capsys
    ):
        """The doc-path and off-diff EXCLUSIONS are gone from this channel,
        because both existed only to keep a FLOOR honest and there is no floor
        here now. A doc-path Critical is surfaced under its own severity —
        which is more information than the old doc-skipped lane gave, not
        less."""
        review = _review(_section(_entry("CHANGELOG.md", "1-2", "🔴 Critical", "Prose")))
        block, _ = self._run(guard_module, monkeypatch, no_inline, [review])
        assert block is False
        assert "[outside-diff Critical] Prose" in capsys.readouterr().err

    def test_a_base_branch_finding_is_surfaced_without_diff_scoping(
        self, guard_module, monkeypatch, no_inline, capsys
    ):
        """#1728's scoping was needed because a base-branch finding must not be
        SCORED against this PR. Nothing in this channel is scored, so there is
        nothing to scope — the finding is simply shown, and the reader decides.
        The `offdiff_lock` opt-in this test used to need is gone with the lane
        it declared."""
        review = _review(_section(_entry("src/not_in_diff.py", "1-2", "🔴 Critical", "Base")))
        block, _ = self._run(guard_module, monkeypatch, no_inline, [review])
        assert block is False
        assert "[outside-diff Critical] Base" in capsys.readouterr().err


class TestQuotedContent:
    """A review body is a code-bearing document — it quotes diffs and embeds
    ```suggestion blocks. Parsing its raw lines fails in BOTH directions, and
    the fail-OPEN one does not announce itself."""

    def test_a_fenced_summary_does_not_reassign_the_current_file(
        self, guard_module, monkeypatch, no_inline
    , capsys):
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
        entries, _, _, _ = guard_module._cr_outside_diff_entries(body)
        assert entries == [("src/a.py", "10-12", "critical", "Real critical")]
        monkeypatch.setenv("_TEST_GH_PR_REVIEW_BODIES", _review(body))
        with no_inline:
            block, _msg = guard_module._check_inline_review_findings("100")
        assert block is False  # advisory channel
        assert "[outside-diff Critical] Real critical (src/a.py:10-12)" in (
            capsys.readouterr().err
        ), "the finding must still be SURFACED against the right file"

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
        entries, _, _, _ = guard_module._cr_outside_diff_entries(_section(first + "\n\n" + second))
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
        entries, _, _, _ = guard_module._cr_outside_diff_entries(body)
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
        entries, _, _, _ = guard_module._cr_outside_diff_entries(body)
        assert entries[0][3] == "", "a distant bold line is not this finding's title"

    def test_a_quoted_bold_line_is_not_borrowed_as_a_title(self, guard_module):
        body = _section(
            "<details>\n<summary>src/a.py (1)</summary><blockquote>\n\n"
            "`1-2`: _cat_ | _🟠 Major_ | _e_\n\n"
            "```md\n**Quoted heading, not the title**\n```\n\n"
            "**The real title**\n\n</blockquote></details>"
        )
        entries, _, _, _ = guard_module._cr_outside_diff_entries(body)
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
        entries, _, _, _ = guard_module._cr_outside_diff_entries(body)
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
        entries, declared, _, _ = guard_module._cr_outside_diff_entries(body)
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
        entries, _, _, _ = guard_module._cr_outside_diff_entries(body)
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
        entries, _, _, _ = guard_module._cr_outside_diff_entries(body)
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
        entries, _, _, _ = guard_module._cr_outside_diff_entries(body)
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
        entries, _, _, _ = guard_module._cr_outside_diff_entries(body)
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
        entries, _, _, _ = guard_module._cr_outside_diff_entries(body)
        assert entries == [("src/a.py", "2-2", "critical", "Blockquoted and safe")]


class TestShortfallFailsClosed:
    """A body declaring more findings than were parsed is an INCOMPLETE read."""

    def test_a_balanced_fence_hiding_a_finding_is_noted(self, guard_module, monkeypatch, no_inline, capsys):
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
        # NOTED, not blocked: an under-report can no longer pass a merge it should
        # have stopped, because there is no merge decision to pass.
        assert block is False
        assert "declares 2 finding(s), parsed 1" in capsys.readouterr().err

    def test_more_parsed_than_declared_is_noted(
        self, guard_module, monkeypatch, no_inline
    , capsys):
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
        assert block is False
        assert "surplus entries indicate prose" in capsys.readouterr().err


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
    def test_a_critical_survives_being_restated(self, guard_module, monkeypatch, no_inline, order, capsys):
        # Reviews arrive oldest-first, so last-write-wins made the reported
        # LEVEL order-dependent: a Critical restated later as Minor was shown
        # as Minor. The channel no longer scores, so the property is now about
        # what the operator is TOLD — which is the entire product of an
        # advisory channel, and just as order-dependent if merged by
        # assignment rather than by MAX.
        self._run(guard_module, monkeypatch, no_inline, order)
        assert "[outside-diff Critical] Same finding" in capsys.readouterr().err

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
        # An advisory NOTE now, and the reasoning that demanded a BLOCK no longer
        # holds: an under-parse was fail-open only because this channel could
        # stop a merge. It cannot, so there is nothing left open.
        assert block is False
        assert "declares 3 finding(s), parsed 1" in capsys.readouterr().err

    def test_no_shortfall_reports_nothing(self, guard_module, monkeypatch, no_inline, capsys):
        body = _section(_entry("src/a.py", "1-2", "🟠 Major", "Parsed"))
        monkeypatch.setenv("_TEST_GH_PR_REVIEW_BODIES", _review(body))
        with no_inline:
            guard_module._check_inline_review_findings("100")
        assert "declares" not in capsys.readouterr().err


class TestFailDirection:
    """An unreadable second channel must block, exactly as the first one does —
    a scan that cannot be read is 'not clean, retry', never a silent pass."""

    def test_unreadable_review_fetch_is_noted(self, guard_module, monkeypatch, no_inline, capsys):
        monkeypatch.setenv("_TEST_GH_PR_REVIEW_BODIES", "{not json")
        with no_inline:
            block, msg = guard_module._check_inline_review_findings("100")
        # A NOTE now: it cannot hide a blocking finding, because this channel has
        # none — and keeping it fail-closed would let a third-party document's
        # formatting hard-block a merge while contributing nothing.
        assert block is False
        assert "UNDER-REPORTING" in capsys.readouterr().err


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
        entries, _, _, _ = guard_module._cr_outside_diff_entries(_section(first + "\n\n" + second))
        assert ("src/a.py", "3-4", "critical", "Real critical") in entries, (
            "a Critical must not be misattributed to the doc path by a "
            "code span the mask failed to close"
        )

    def test_a_surplus_critical_reports_the_surplus_not_the_finding(
        self, guard_module, monkeypatch, no_inline
    , capsys):
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
        assert block is False
        err = capsys.readouterr().err
        assert "surplus entries indicate prose" in err
        assert "PhantomOrReal" not in err, (
            "a quarantined batch must not be surfaced as a real finding"
        )


class TestRoundTwoFindings:
    """Codex round 2. The first is a FAIL-OPEN on the floor, so it is fixed in
    the terminal push rather than accepted; the second costs nothing to fix and
    makes the report state what the reviewer actually said."""

    def test_an_html_escaped_path_is_decoded_before_it_is_used_as_an_identity(
        self, guard_module, monkeypatch, no_inline, capsys
    ):
        # VERIFY-RED: the extracted path is compared against the RAW path
        # GitHub returns. CodeRabbit escapes markup-significant characters
        # inside the summary, so `docs/Q&amp;A.md` never matched `docs/Q&A.md`
        # and the finding was routed to the non-scoring off-diff lane —
        # a Critical read correctly, then attributed to nobody, and the merge
        # allowed. `&` is the character this actually happens with.
        monkeypatch.setenv(
            "_TEST_GH_PR_FILES",
            json.dumps({"filename": "src/a&b.py", "previous_filename": None}),
        )
        # A SOURCE path deliberately: a `docs/*.md` path would be doc-skipped
        # for a legitimate reason and the test would pass without proving the
        # decode mattered.
        body = _section(
            "<details>\n<summary>src/a&amp;b.py (1)</summary><blockquote>\n\n"
            "`1-2`: _cat_ | _🔴 Critical_ | _e_\n\n**Escaped path**\n\n"
            "</blockquote></details>"
        )
        entries, _, _, _ = guard_module._cr_outside_diff_entries(body)
        assert entries[0][0] == "src/a&b.py", "the path is an identity, not display text"

        monkeypatch.setenv("_TEST_GH_PR_REVIEW_BODIES", _review(body))
        with no_inline:
            block, msg = guard_module._check_inline_review_findings("100")
        assert block is False  # advisory channel
        assert "[outside-diff Critical] Escaped path (src/a&b.py:1-2)" in (
            capsys.readouterr().err
        ), "the decoded path must reach the report, not an escaped spelling"

    @pytest.mark.parametrize(
        ("token", "shown"),
        [("Info", "Info"), ("Trivial", "Trivial"), ("🟡 Minor", "Minor")],
    )
    def test_the_report_names_the_level_the_reviewer_gave(
        self, guard_module, monkeypatch, no_inline, capsys, token, shown
    ):
        # VERIFY-RED for Info/Trivial: every below-Major level printed as
        # "Minor". Same score (0.0) either way — but the report is an
        # inventory, and rounding a level UP overstates the reviewer.
        review = _review(_section(_entry("src/a.py", "1-2", token, "Low sev")))
        monkeypatch.setenv("_TEST_GH_PR_REVIEW_BODIES", review)
        with no_inline:
            block, _ = guard_module._check_inline_review_findings("100")
        assert block is False
        assert f"[outside-diff {shown}]" in capsys.readouterr().err


class TestBlockquotedFenceInversion:
    """A container marker is not noise — normalising it away unscopes fences.

    THE DEFECT (adversarial audit, PR #1847). `_cr_normalize_review_body`
    strips blockquote prefixes so the mask can see structure that quoting hid.
    That is right, and it was also lossy in a way nothing noticed: a renderer
    scopes a fence to its CONTAINER, so a `> ``` ` line inside a document-level
    fenced block is ordinary CONTENT — the `>` is not indentation, so it cannot
    close anything. Stripped first, it becomes a valid closer, and the mask
    INVERTS: the fence ends early, the real closer opens a phantom one, and
    everything to the next delimiter is masked — including the section header
    that carries the declared count. `declared` and `parsed` are then both 0,
    so the reconciliation backstop is blind by construction and NO canary
    fires. Measured end-to-end: a floor-class Critical stopped blocking.
    """

    def _surfaced(self, guard_module, monkeypatch, no_inline, capsys, body) -> str:
        """Everything the guard reported for this body. The observable moved
        from the VERDICT to the REPORT when the channel became advisory — the
        parser property is unchanged, and so is what a mis-read costs: a
        finding attributed to the wrong file, or lost entirely."""
        monkeypatch.setenv(
            "_TEST_GH_PR_FILES",
            json.dumps({"filename": "src/a.py", "previous_filename": None}),
        )
        monkeypatch.setenv("_TEST_GH_PR_REVIEW_BODIES", _review(body))
        with no_inline:
            guard_module._check_inline_review_findings("100")
        return capsys.readouterr().err

    # The trailing fence is load-bearing, not decoration: without it the
    # phantom fence never closes, the unclosed-construct recovery clears the
    # mask, and the defect hides behind that recovery. A fixture that omits it
    # passes with and without the fix — proving nothing.
    _TAIL = "\n\n```\ntrailing\n```\n"

    def _body(self, quoted_delimiter: bool) -> str:
        middle = "> ```" if quoted_delimiter else "plain"
        section = _section(_entry("src/a.py", "10-12", "🔴 Critical", "Real critical"))
        return f"```md\n{middle}\nX\n```\n\n{section}{self._TAIL}"

    def test_a_blockquoted_delimiter_does_not_close_a_document_level_fence(
        self, guard_module, monkeypatch, no_inline, capsys
    ):
        # VERIFY-RED: dropping `depth == fence_bq` from the close condition
        # loses the finding entirely here, while the control below keeps it.
        err = self._surfaced(guard_module, monkeypatch, no_inline, capsys, self._body(True))
        assert "[outside-diff Critical] Real critical" in err, (
            "the finding must survive a body that quotes a fence delimiter "
            "inside a fenced block"
        )

    def test_the_control_surfaces_it_too(self, guard_module, monkeypatch, no_inline, capsys):
        """The pair is the evidence: without a control that also surfaces the
        finding, a green result cannot distinguish the fix from a fixture that
        never exercised the mask at all."""
        err = self._surfaced(guard_module, monkeypatch, no_inline, capsys, self._body(False))
        assert "[outside-diff Critical] Real critical" in err

    def test_a_quoted_delimiter_leaves_the_fence_open_in_the_mask(self, guard_module):
        """The same property one layer down, where it is directly observable:
        the lines after the quoted delimiter stay masked."""
        text, depths = guard_module._cr_normalize_review_body("```md\n> ```\nX\n```\nafter\n")
        fence_mask, _ = guard_module._cr_masks(text, depths)
        assert depths == [0, 1, 0, 0, 0, 0], "the blockquote depth must survive stripping"
        assert fence_mask[:4] == [True, True, True, True], (
            "the quoted delimiter is CONTENT, so the fence spans through the real closer"
        )
        assert fence_mask[4] is False, "and ends after it"

    def test_the_mask_is_unchanged_for_callers_that_pass_no_depths(self, guard_module):
        """`_cr_masks`'s other caller reads text no quote prefix was stripped
        from. Depths default to zero there, which must be exactly the old
        behaviour — a shared mask that changed under one caller would be the
        two-implementations bug this parser already paid for once."""
        body = "```md\nX\n```\nafter\n"
        assert guard_module._cr_masks(body) == guard_module._cr_masks(body, None)


class TestSectionDepthDrift:
    """A depth pin that cannot notice when it stops holding is a vacuous green.

    `_CR_SECTION_DEPTH` is a MEASURED constant of a document nobody controls
    (23/23 live bodies put the section at depth 1). The count reconciliation
    cannot backstop it, because `declared` is read from the SAME match that
    licenses parsing: a section one `<details>` deeper yields declared ==
    parsed == 0 — a clean read, silently, on every PR (adversarial audit,
    PR #1847).
    """

    def test_a_section_nested_one_level_deeper_is_reported_not_ignored(
        self, guard_module, monkeypatch, no_inline, capsys
    ):
        # VERIFY-RED: without the canary this returns block=False with an empty
        # message — the whole channel silently switched off by third-party
        # layout drift.
        nested = (
            "<details>\n<summary>Wrapper</summary><blockquote>\n\n"
            + _section(_entry("src/a.py", "1-2", "🔴 Critical", "Hidden"))
            + "\n\n</blockquote></details>"
        )
        entries, declared, _, drift = guard_module._cr_outside_diff_entries(nested)
        assert (entries, declared) == ([], 0), "precondition: the section is not matched"
        assert drift, "and that must be REPORTED rather than read as a clean body"

        monkeypatch.setenv("_TEST_GH_PR_REVIEW_BODIES", _review(nested))
        with no_inline:
            block, _msg = guard_module._check_inline_review_findings("100")
        assert block is False  # advisory: drift is REPORTED, never a block
        assert "depth" in capsys.readouterr().err

    def test_a_section_at_the_expected_depth_reports_no_drift(self, guard_module):
        """The negative control. Without it, a canary that fired on every body
        would pass the test above while blocking every merge."""
        body = _section(_entry("src/a.py", "1-2", "🟡 Minor", "Ordinary"))
        entries, _, _, drift = guard_module._cr_outside_diff_entries(body)
        assert entries and not drift

    def test_a_section_name_in_prose_at_the_wrong_depth_is_not_drift(self, guard_module):
        """The canary keys on real STRUCTURE, so prose mentioning the section
        cannot trip it — otherwise a finding that quotes the section name would
        block the merge on its own description."""
        body = _section(
            "<details>\n<summary>src/a.py (1)</summary><blockquote>\n\n"
            "`1-2`: _cat_ | _🟡 Minor_ | _e_\n\n**Talks about it**\n\n"
            "See the Outside diff range comments section for context.\n\n"
            "</blockquote></details>"
        )
        entries, _, _, drift = guard_module._cr_outside_diff_entries(body)
        assert entries and not drift


def test_every_surfaced_finding_is_printed(guard_module, monkeypatch, no_inline, capsys):
    """No display cap on this channel — the inventory IS the output.

    The inline channel can afford one, because its findings also reach a score
    and a clipped list still blocks. Here nothing scores, so an entry dropped
    for display is a finding that never reaches the operator at all: cutting
    the only copy, which is the amputation CLAUDE.md forbids.

    VERIFY-RED: restore any of the `[:5]` / `[:8]` slices and this fails.
    """
    entries = "\n".join(
        _entry(f"src/f{i}.py", f"{i}-{i}", "🟠 Major", f"Major finding {i}") for i in range(9)
    )
    monkeypatch.setenv(
        "_TEST_GH_PR_FILES",
        "\n".join(
            json.dumps({"filename": f"src/f{i}.py", "previous_filename": None})
            for i in range(9)
        ),
    )
    monkeypatch.setenv("_TEST_GH_PR_REVIEW_BODIES", _review(_section(entries)))
    with no_inline:
        guard_module._check_inline_review_findings("100")
    err = capsys.readouterr().err
    missing = [i for i in range(9) if f"Major finding {i}" not in err]
    assert not missing, f"findings dropped from the only place they appear: {missing}"
