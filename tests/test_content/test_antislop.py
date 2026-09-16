"""Tests for the deterministic anti-slop scrubber (genesis.content.antislop)."""

from __future__ import annotations

import pytest

from genesis.content.antislop import detect, scrub

EM = "—"  # — em dash
EN = "–"  # – en dash


class TestScrubEmDashFix:
    def test_spaced_em_dash_is_rewritten_to_double_hyphen(self):
        # Owner ruling 2026-09-16: published prose never uses an em dash; the
        # prescribed dash form is two hyphens closed up (word--word). The true
        # em dash reads typeset rather than typed. The scrubber therefore fixes
        # the #1 tell TO the prescribed form, not to a bare em dash.
        r = scrub(f"Genesis remembers {EM} that's the point.")
        assert r.cleaned_text == "Genesis remembers--that's the point."
        assert r.fixes_applied == ["em_dash:1"]

    def test_counts_multiple_em_dashes(self):
        r = scrub(f"a {EM} b {EM} c")
        assert r.cleaned_text == "a--b--c"
        assert r.fixes_applied == ["em_dash:2"]

    def test_bare_em_dash_is_also_rewritten(self):
        # Published prose never uses an em dash (owner ruling 2026-09-16),
        # and the superseded guidance actively prescribed this exact form --
        # so it is the highest-probability residual em dash in real drafts.
        r = scrub(f"word{EM}word")
        assert r.cleaned_text == "word--word"
        assert r.fixes_applied == ["em_dash:1"]

    def test_em_dash_fix_applies_even_when_not_voiced(self):
        r = scrub(f"a {EM} b", is_voiced=False)
        assert r.cleaned_text == "a--b"
        assert r.fixes_applied == ["em_dash:1"]
        assert r.flags == []


class TestAmbiguousDashesAreFlaggedNotRewritten:
    def test_en_dash_range_is_not_rewritten(self):
        text = f"scores ran 5 {EN} 10 last week"
        r = scrub(text)
        assert r.cleaned_text == text  # unchanged
        assert r.fixes_applied == []
        assert any("spaced_ambiguous_dash" in f for f in r.flags)

    def test_double_hyphen_is_not_rewritten(self):
        text = "pass the value -- carefully -- through"
        r = scrub(text)
        assert r.cleaned_text == text
        assert r.fixes_applied == []
        assert any("spaced_ambiguous_dash" in f for f in r.flags)

    def test_markdown_table_rule_is_not_mangled(self):
        # The architect's Issue 5: a 2-dash table cell must survive.
        text = "| name | -- | value |"
        r = scrub(text)
        assert r.cleaned_text == text


class TestCodeRegionsExcluded:
    def test_fenced_block_dashes_untouched(self):
        text = f"before\n```\nx {EM} y\na -- b\n```\nafter"
        r = scrub(text)
        assert r.cleaned_text == text
        assert r.fixes_applied == []
        assert r.flags == []

    def test_inline_code_untouched(self):
        text = f"run `x {EM} y` now"
        r = scrub(text)
        assert r.cleaned_text == text
        assert r.fixes_applied == []

    def test_em_dash_in_prose_fixed_even_with_code_present(self):
        text = f"intro {EM} here\n```\ncode -- block\n```"
        r = scrub(text)
        assert r.cleaned_text == "intro--here\n```\ncode -- block\n```"
        assert r.fixes_applied == ["em_dash:1"]

    def test_em_dash_abutting_inline_code_is_rewritten(self):
        # The widened rule needs no flanking spaces, so a dash abutting a code
        # span rewrites within its own prose span; the count comes from the
        # per-span rewrite, never from a code-blanked view.
        r = scrub(f"see {EM}`x` now")
        assert r.cleaned_text == "see--`x` now"
        assert r.fixes_applied == ["em_dash:1"]

    def test_banned_word_inside_code_not_flagged(self):
        r = scrub("call `navigate()` to move", is_voiced=True)
        assert not any("banned_words" in f for f in r.flags)


class TestBannedWordFlagging:
    def test_banned_word_flagged_when_voiced_text_unchanged(self):
        text = "We leverage robust synergy to win."
        r = scrub(text, is_voiced=True)
        assert r.cleaned_text == text  # never deleted
        assert r.fixes_applied == []
        banned = next((f for f in r.flags if "banned_words" in f), None)
        assert banned is not None
        assert "leverage" in banned and "robust" in banned and "synergy" in banned

    def test_banned_word_not_flagged_when_not_voiced(self):
        r = scrub("We leverage robust synergy.", is_voiced=False)
        assert r.flags == []
        assert r.fixes_applied == []

    def test_banned_phrase_flagged(self):
        r = scrub("It's worth noting that this matters.", is_voiced=True)
        assert any("banned_phrases" in f for f in r.flags)


class TestCleanText:
    def test_clean_text_no_fixes_no_flags(self):
        r = scrub("Short and plain. Nothing fancy here.", is_voiced=True)
        assert r.fixes_applied == []
        assert r.flags == []
        assert r.clean is True

    def test_clean_property_false_when_fix_applied(self):
        assert scrub(f"a {EM} b").clean is False


class TestDetect:
    def test_detect_empty_for_clean_text(self):
        assert detect("A simple, honest sentence.") == {}

    def test_first_person_opening_is_allowed(self):
        findings = detect("I keep coming back to this because it matters.")
        assert "opens_with_I" not in findings
        assert not any("opens_with_I" in flag for flag in scrub("I keep coming back to this because it matters.").flags)

    def test_detect_reports_categories(self):
        text = f"We delve into the landscape {EM} a testament to synergy."
        f = detect(text)
        assert f["em_dash"] == 1
        assert "delve" in f["banned_words"]
        assert "landscape" in f["banned_words"]

    def test_detect_excludes_code(self):
        assert detect(f"```\nwe leverage {EM} robust\n```") == {}

    def test_detect_contrast_cadence(self):
        f = detect("It's not about speed, it's about depth.")
        assert "contrast_structures" in f


class TestRealisticSlopRoundTrip:
    def test_published_post_style_em_dash_leak_is_fixed(self):
        # The bug class that shipped: a spaced em dash in voiced content.
        draft = (
            f"Genesis is here {EM} not the sci-fi version. "
            "It remembers everything and learns from every interaction."
        )
        r = scrub(draft, is_voiced=True)
        assert f" {EM} " not in r.cleaned_text
        assert "here--not" in r.cleaned_text
        assert r.fixes_applied == ["em_dash:1"]


class TestFirstPersonOpenerAllowed:
    """First person is encouraged by the voice-master style-guide, so an opener
    starting with "I" is NOT an anti-slop tell (the old opens_with_I gate was
    removed to stop it contradicting the guidance)."""

    def test_detect_does_not_flag_first_person_opener(self):
        assert "opens_with_I" not in detect(
            "I keep coming back to the same failure mode."
        )

    def test_scrub_does_not_flag_first_person_opener(self):
        r = scrub(
            "I keep coming back to it. The failure bites every time, in new ways.",
            is_voiced=True,
        )
        assert not any("opens_with_I" in f for f in r.flags)


class TestOutputIsNotSelfFlagged:
    """The rewrite emits ``--``, which IS a member of _FLAG_DASHES.

    It is emitted unspaced, and _prose_only blanks code regions with a
    NON-WHITESPACE placeholder -- so a code span next to the rewrite can no
    longer manufacture the flanking whitespace that turned the fixer into a
    self-flagger (a measured regression in an earlier draft of this change).
    """

    def test_rewrite_between_code_spans_is_not_self_flagged(self):
        r = scrub(f"`a` {EM} `b`")
        assert r.cleaned_text == "`a`--`b`"
        assert r.fixes_applied == ["em_dash:1"]
        assert r.flags == []

    def test_human_authored_unspaced_double_hyphen_near_code_not_flagged(self):
        # Same shape typed by a person: the prescribed form must never flag.
        r = scrub("`a`--`b` explains it")
        assert r.fixes_applied == []
        assert r.flags == []

    def test_scrub_is_idempotent_in_text_fixes_and_flags(self):
        for src in (f"a {EM} b", f"`a` {EM} `b`", f"x {EM} `c` {EM} y", f"w{EM}w"):
            first = scrub(src)
            second = scrub(first.cleaned_text)
            assert second.cleaned_text == first.cleaned_text
            assert second.fixes_applied == []
            assert second.flags == first.flags

    def test_genuine_spaced_double_hyphen_still_flagged(self):
        # Positive control: the ambiguous-dash detector must still work, or
        # these green tests prove nothing about the placeholder change.
        r = scrub("run -- foo to pass args")
        assert any("spaced_ambiguous_dash" in f for f in r.flags)
# ── Findings from the cross-model review of the dash ruling ────────────────
#
# Widening the em dash rewrite from the SPACED form to the BARE form changed
# two properties that the spaced-only pattern had made safe for free:
#   1. a dash with no surrounding whitespace became rewritable, which is
#      exactly the shape a dash takes inside a URL, an email or a path;
#   2. the rewrite began to GROW text (one char to two) where it had only
#      ever shrunk it (three chars to two).
# Each test below has a control, so a protection that swallowed everything
# would fail rather than look correct.


class TestStructuredTokensAreNotRewritten:
    """A dash inside a structured token is data, not punctuation."""

    @pytest.mark.parametrize(
        "text",
        [
            "See [notes](https://example.com/foo—bar) now.",
            "Mail first—last@example.com today.",
            "The file is /var/log/app—old.log on disk.",
            "Grab https://ex.io/a—b?q=1#frag now.",
        ],
    )
    def test_structured_token_survives_untouched(self, text):
        assert scrub(text).cleaned_text == text

    @pytest.mark.parametrize(
        "text",
        [
            "Genesis remembers—that is the point.",
            "alpha — beta gamma.",
            "word—word and more—text here.",
            "Read docs/guide.md—then continue.",
        ],
    )
    def test_control_ordinary_prose_is_still_rewritten(self, text):
        """Without this, a protection matching everything would pass above."""
        out = scrub(text).cleaned_text
        assert "—" not in out
        assert "--" in out


class TestPlaceholderIsNotCountedAsAWord:
    """The protected-region placeholder must not shift sentence lengths."""

    def test_code_span_does_not_inflate_its_sentence(self):
        from genesis.content.antislop import (
            _prose_only,
            _prose_word_count,
            _sentences,
        )

        text = "`x` one. a b c d. a b c d. a b c d e."
        lengths = [_prose_word_count(s) for s in _sentences(_prose_only(text))]
        assert lengths == [1, 4, 4, 5]

    def test_uniform_sentence_length_not_falsely_flagged(self):
        text = "`x` one. a b c d. a b c d. a b c d e."
        assert "uniform_sentence_length" not in detect(text)

    def test_control_genuinely_uniform_text_still_flags(self):
        """The detector must still be alive after the placeholder change."""
        text = "a b c d. e f g h. i j k l. m n o p."
        assert "uniform_sentence_length" in detect(text)

    def test_placeholder_adjacent_to_a_word_still_counts_once(self):
        from genesis.content.antislop import (
            _prose_only,
            _prose_word_count,
            _sentences,
        )

        text = "`x`y alone."
        assert [_prose_word_count(s) for s in _sentences(_prose_only(text))] == [2]
