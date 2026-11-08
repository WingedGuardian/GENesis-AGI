"""clip_words / first_sentence: shorten display text at a word boundary."""

from __future__ import annotations

import pytest

from genesis.util.text import ELLIPSIS, clip_words, first_sentence


def test_text_that_fits_is_returned_unchanged():
    assert clip_words("short text.", 40) == "short text."
    assert clip_words("exactly ten", 11) == "exactly ten"
    # Trailing punctuation is only stripped when a cut happened.
    assert clip_words("ends with a dot.", 16) == "ends with a dot."


def test_cuts_at_the_last_word_boundary_and_marks_it():
    text = "nothing leaves Genesis without an explicit approval"
    out = clip_words(text, 20)
    assert out == "nothing leaves…"
    assert len(out) <= 20


def test_result_never_exceeds_the_limit_including_the_marker():
    text = "alpha beta gamma delta epsilon zeta eta theta iota kappa"
    for n in range(1, len(text)):
        out = clip_words(text, n)
        assert len(out) <= n, (n, out)
        assert out.endswith(ELLIPSIS)


def test_a_boundary_right_at_the_limit_keeps_the_whole_word():
    # "alpha beta" is 10 chars; with room for the marker the cut lands on the
    # space after "alpha beta" when n is 11.
    assert clip_words("alpha beta gamma", 11) == "alpha beta…"


def test_never_cuts_mid_word():
    text = "the quick brown fox jumps over the lazy dog again and again"
    for n in range(6, len(text)):
        out = clip_words(text, n)
        body = out[: -len(ELLIPSIS)]
        # Every kept word is a whole word of the original.
        assert set(body.split()) <= set(text.split()), (n, out)


def test_single_word_longer_than_limit_is_hard_cut_with_marker():
    out = clip_words("x" * 400, 250)
    assert out == "x" * 249 + ELLIPSIS
    assert len(out) == 250


def test_a_long_first_word_followed_by_more_text_is_hard_cut():
    out = clip_words("supercalifragilistic word", 10)
    assert out == "supercali" + ELLIPSIS


def test_trailing_punctuation_and_whitespace_are_stripped_before_the_marker():
    assert clip_words("first clause, second clause here", 16) == "first clause…"
    assert clip_words("done.  then more words follow", 9) == "done…"
    assert clip_words("a list: one two three four", 9) == "a list…"
    assert clip_words("dash - then the rest of it", 8) == "dash…"


def test_reclipping_clipped_parts_never_doubles_the_marker():
    joined = "first part ends here…; second part also clipped…; third"
    out = clip_words(joined, 24)
    assert out == "first part ends here…"
    assert "……" not in clip_words(joined, 50)


def test_closing_brackets_and_quotes_are_kept():
    assert clip_words('said "hello" then left the room', 14) == 'said "hello"…'
    assert clip_words("a call (foo) and then more", 14) == "a call (foo)…"


def test_unicode_text_is_counted_in_characters_and_cut_on_words():
    text = "café naïve résumé über straße jalapeño"
    out = clip_words(text, 20)
    assert out == "café naïve résumé…"
    assert len(out) <= 20
    # A run without spaces (e.g. CJK) is one long word: hard cut.
    assert clip_words("日本語のテキストです", 5) == "日本語の" + ELLIPSIS
    # Non-ASCII whitespace is a boundary too.
    assert clip_words("one two three", 9) == "one two…"


def test_tiny_limits():
    assert clip_words("hello world", 1) == ELLIPSIS
    assert clip_words("hello world", 0) == ""
    assert clip_words("", 5) == ""


def test_newlines_count_as_word_boundaries():
    assert clip_words("line one\nline two continues", 12) == "line one…"


def test_a_hard_cut_skips_leading_whitespace():
    # A leading newline kept by the hard cut would break a markdown bullet.
    assert clip_words("\n\n\nabcdefgh ijk", 5) == "abcd…"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("One sentence. Another one.", "One sentence."),
        ("No terminator here", "No terminator here"),
        ("Ship v3.0 to the fleet. Then rest.", "Ship v3.0 to the fleet."),
        ("Is it done? Yes.", "Is it done?"),
        ("  leading space. tail", "leading space."),
        ("", ""),
    ],
)
def test_first_sentence(text, expected):
    assert first_sentence(text) == expected
