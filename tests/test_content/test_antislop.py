"""Tests for the deterministic anti-slop scrubber (genesis.content.antislop)."""

from __future__ import annotations

from genesis.content.antislop import detect, scrub

EM = "—"  # — em dash
EN = "–"  # – en dash


class TestScrubEmDashFix:
    def test_spaced_em_dash_is_rewritten_to_bare(self):
        r = scrub(f"Genesis remembers {EM} that's the point.")
        assert r.cleaned_text == f"Genesis remembers{EM}that's the point."
        assert r.fixes_applied == ["spaced_em_dash:1"]

    def test_counts_multiple_em_dashes(self):
        r = scrub(f"a {EM} b {EM} c")
        assert r.cleaned_text == f"a{EM}b{EM}c"
        assert r.fixes_applied == ["spaced_em_dash:2"]

    def test_correctly_set_em_dash_is_left_alone(self):
        # No flanking spaces -> not a tell, not touched.
        text = f"word{EM}word"
        r = scrub(text)
        assert r.cleaned_text == text
        assert r.fixes_applied == []

    def test_em_dash_fix_applies_even_when_not_voiced(self):
        r = scrub(f"a {EM} b", is_voiced=False)
        assert r.cleaned_text == f"a{EM}b"
        assert r.fixes_applied == ["spaced_em_dash:1"]
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
        assert r.cleaned_text == f"intro{EM}here\n```\ncode -- block\n```"
        assert r.fixes_applied == ["spaced_em_dash:1"]

    def test_em_dash_abutting_inline_code_not_falsely_reported(self):
        # An em dash directly against inline code can't be collapsed at the span
        # boundary; fixes_applied must equal what was actually rewritten (0), not
        # over-report from a code-blanked count.
        text = f"see {EM}`x` now"
        r = scrub(text)
        assert r.fixes_applied == []
        assert r.cleaned_text == text

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

    def test_detect_reports_categories(self):
        text = f"We delve into the landscape {EM} a testament to synergy."
        f = detect(text)
        assert f["spaced_em_dash"] == 1
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
        assert f"here{EM}not" in r.cleaned_text
        assert r.fixes_applied == ["spaced_em_dash:1"]
