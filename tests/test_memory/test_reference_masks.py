"""The masking engine of a held-out credential value (#2426), on its own.

``reference_masks`` masks every span of a held value in the fields a credential
row indexes (its identifier and tags) with ``[held]``. These tests call it
directly, without the ingest chokepoint.

Fake credentials only.
"""

from __future__ import annotations

import time

import pytest

from genesis.memory import reference_masks as rm
from genesis.memory.reference_extraction import captured_secrets

_SECRET = "Hunter2024x!fake"
_VALUE = f"password: {_SECRET}"


# ─── masking in the original text ────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "masked"),
    [
        ("wifi Ｈｕｎｔｅｒ２０２４ｘ！ｆａｋｅ upstairs", "wifi [held] upstairs"),  # fullwidth
        ("wifi Hun​ter2024x!fake upstairs", "wifi [held] upstairs"),  # zero-width
        ("wifi Hunter2024x%21fake upstairs", "wifi [held] upstairs"),  # percent-encoded
        ("wifi HUNTER2024X!FAKE-old", "wifi [held]-old"),  # another case
        ("nothing here", "nothing here"),
    ],
)
def test_a_span_is_masked_in_the_original_text(text, masked):
    assert rm.mask_value_spans(_VALUE, text) == masked


_WORDS = "correct horse battery staple"


@pytest.mark.parametrize(
    "gap",
    ["  ", "\t", "\n", " \t ", " ", "  ", "\r\n"],
    ids=["double-space", "tab", "newline", "mixed", "nbsp", "nbsp-space", "crlf"],
)
def test_any_whitespace_run_inside_a_multi_word_value_is_masked(gap):
    strict, _named = rm.value_masks(_WORDS)
    original = f"correct{gap}horse battery{gap}staple"
    text = f"tag {original} end"
    masked, spans = strict.mask_and_spans(text)
    assert masked == "tag [held] end"
    assert spans == [original]


_ALPHABET = [*"abcXYZ019!@", " ", "  ", "\t", "\n", "\r", " ", " ", "é", "Ｈ", "​"]


def test_normalised_with_origins_agrees_with_normalise():
    """The span matcher's per-character form is the form ``_normalise`` gives
    (whitespace runs read as one space), and every origin points into the
    text, in order."""
    import random

    rng = random.Random(2426)
    for _ in range(500):
        text = "".join(rng.choice(_ALPHABET) for _ in range(rng.randint(0, 24)))
        normalised, origins = rm._normalised_with_origins(text)
        assert normalised == rm._normalise(text), repr(text)
        assert len(origins) == len(normalised)
        assert list(origins) == sorted(origins)
        assert all(0 <= o < len(text) for o in origins)


def test_a_percent_encoded_value_is_masked_whole():
    strict, _named = rm.value_masks("Demo@Fake123!")
    assert strict.mask("x Demo@Fake123! y") == "x [held] y"
    assert strict.mask("x Demo%40Fake123%21 y") == "x [held] y"
    assert strict.mask("x Demo%2540Fake123%2521 y") == "x [held] y"  # encoded twice
    assert strict.mask("x path%2Fto Demo%40Fake123%21 y") == "x path%2Fto [held] y"
    assert strict.mask("x path%2Fto%20here y") == "x path%2Fto%20here y"


def test_nothing_to_mask_leaves_the_text_as_it_is():
    assert rm.mask_value_spans("", "lab wifi") == "lab wifi"
    assert rm.mask_value_spans("   ", "lab wifi") == "lab wifi"
    assert rm.mask_value_spans(_VALUE, "") == ""


def test_mask_and_spans_reports_what_it_replaced():
    mask = rm.ValueMask(_VALUE)
    assert mask.mask_and_spans(f"lab {_SECRET} wifi") == ("lab [held] wifi", [_SECRET])
    assert mask.mask_and_spans("lab wifi") == ("lab wifi", [])


def test_a_field_is_masked_at_any_nesting_and_keeps_its_shape():
    mask = rm.ValueMask(_SECRET)
    field = {"k": [f"x {_SECRET}"], _SECRET: 1, "n": None, "t": ("a",)}
    assert mask.mask_field(field) == {"k": ["x [held]"], "[held]": 1, "n": None, "t": ("a",)}
    assert mask.mask_field(None) is None
    assert mask.mask_field(42) == 42
    # A non-string scalar whose text carries the value has nothing else to keep.
    assert rm.ValueMask("12345678").mask_field(12345678) == rm.HELD_MASK


def test_a_tag_that_was_only_the_value_is_dropped():
    mask = rm.ValueMask(_SECRET)
    assert rm._mask_tag(mask, _SECRET) is None
    assert rm._mask_tag(mask, f"lab {_SECRET}") == "lab [held]"
    assert rm._mask_tag(mask, "lab") == "lab"


# ─── the probe sets a write builds ───────────────────────────────────────────


def test_a_delimited_value_masks_a_term_shaped_secret():
    _strict, named = rm.value_masks("Sunny-2024")
    assert named.mask("router Sunny-2024") == "router [held]"


def test_a_prose_value_keeps_its_term_shaped_words():
    _strict, named = rm.value_masks("The router uses WPA2-PSK on the lab band", prose=True)
    assert named.mask("router WPA2-PSK login") == "router WPA2-PSK login"


def test_a_captured_secret_in_prose_is_masked_even_when_term_shaped():
    prose = "The router uses Blue-Horse daily. password: Blue-Horse"
    assert captured_secrets([prose, None, ""]) == {"Blue-Horse"}
    _strict, named = rm.value_masks(prose, prose=True)
    assert named.mask_and_spans("router Blue-Horse x") == ("router [held] x", ["Blue-Horse"])


def test_the_prose_exemption_is_decided_by_each_probes_source():
    """Comment 4576838396: a known held value passed through ``also`` is masked
    even when the incoming text is prose; it is exempt only when its own source
    is prose too."""
    prose = "Sunny-2024 shows up in this prose"
    _strict, named = rm.value_masks(prose, prose=True, also=(("Sunny-2024", None),))
    assert named.mask("router Sunny-2024") == "router [held]"
    _strict, named = rm.value_masks(
        prose, prose=True, also=(("Sunny-2024 is the old one", None, True),)
    )
    assert named.mask("router Sunny-2024") == "router Sunny-2024"


def test_only_the_identifier_exempts_the_login_of_a_login_value():
    strict, named = rm.value_masks("admin9x / Pw9fake!x")
    assert named.mask("admin9x router") == "admin9x router"
    assert named.mask("Pw9fake!x") == "[held]"
    assert strict.mask("admin9x") == "[held]"


# ─── caps ────────────────────────────────────────────────────────────────────


def test_a_cap_never_cuts_through_a_mask():
    assert rm.cap_masked("ab [held] cd", 5) == "ab "
    assert rm.cap_masked("abcdef", 3) == "abc"
    assert rm.cap_masked("short", 64) == "short"


def test_a_tag_is_capped_as_stored():
    assert len(rm.cap_tag("t" * 300)) == rm.MAX_TAG_CHARS
    assert rm.cap_tag({"k": "v"}) == {"k": "v"}
    capped = rm.cap_tag({"k": "x" * 300})
    assert isinstance(capped, str) and len(capped) == rm.MAX_TAG_CHARS


def test_a_cap_never_uncovers_a_whole_word_value():
    """A cut can make a match: "tigers" carries no whole-word "tiger", but its
    cut "tiger" does. Given the mask, the cap masks the cut again."""
    mask = rm.ValueMask("tiger")
    text = "x" * 250 + " tigers"
    assert mask.mask(text) == text
    assert mask.carries(rm.cap_masked(text, 256))  # the bare cut uncovers it
    capped = rm.cap_masked(text, 256, mask)
    assert len(capped) <= 256 and not mask.carries(capped)
    assert capped == "x" * 250 + " "


def test_a_capped_tag_never_uncovers_a_whole_word_value():
    mask = rm.ValueMask("tiger")
    tag = "y" * 250 + " tigers"
    capped = rm.cap_tag(tag, mask)
    assert len(capped) <= rm.MAX_TAG_CHARS and not mask.carries(capped)
    # As JSON text: the cut lands just after "tiger".
    field = {"k": "y" * 243 + " tigers"}
    assert mask.carries(rm.cap_tag(field))
    capped = rm.cap_tag(field, mask)
    assert isinstance(capped, str) and len(capped) <= rm.MAX_TAG_CHARS
    assert not mask.carries(capped)


def test_a_run_of_tags_spelling_the_value_is_masked_as_one_tag():
    strict, _named = rm.value_masks("correct horse, battery staple")
    tags = rm.clean_tag_runs(
        strict, ["correct horse", "battery staple", "other"], str, "Tags: ", ""
    )
    assert tags == [rm.HELD_MASK, "other"]


# ─── bounded cost ────────────────────────────────────────────────────────────


def test_two_thousand_encoded_copies_mask_in_linear_time():
    text = " ".join(["Hunter2024x%21fake"] * 2000)
    started = time.perf_counter()
    masked = rm.mask_value_spans(_VALUE, text)
    assert time.perf_counter() - started < 2.0
    assert masked == " ".join([rm.HELD_MASK] * 2000)


_UPLOAD_LINES = [
    f"router {i % 7} uses port 8443 on db.example.org token=Tk{i % 500}x!z" for i in range(36000)
]


def test_a_two_megabyte_value_is_probed_and_masked_quickly():
    """Review M2: the probes of a 2 MB held value are built once and every
    field is masked in time linear in the field."""
    text = "\n".join(_UPLOAD_LINES)
    assert len(text) > 1_800_000
    started = time.perf_counter()
    strict, labels = rm.value_masks(text, prose=True)
    assert labels.mask("creds Tk7x!z.txt") == "creds [held].txt"
    assert strict.mask_field({"source_doc": "/uploads/creds.txt"}) == {
        "source_doc": "/uploads/creds.txt"
    }
    assert time.perf_counter() - started < 2.0


def test_masking_sixty_four_adversarial_tags_is_fast():
    """The serialised check is one pass per match, not a window search: the
    three worst cases together take well under a second (the window search
    took about 11 s for them)."""
    tag = "b" * 256
    start = time.perf_counter()
    for span in (8, 32, 63):
        strict, _ = rm.value_masks('", "'.join([tag] * span))
        out = rm._serialised_clean(strict, [tag] * 64)
        assert out is None or not strict.carries(out)
    assert time.perf_counter() - start < 1.0


def test_masking_a_name_against_a_repeated_token_value_is_bounded():
    """Round 2 SF1: the run search over a held value bounds the occurrences it
    examines and its look-back, so a 400 KB repeated-token value masks a name
    in well under a second (the unbounded search took seconds)."""
    value = "Abcd1234 " * 45000
    _strict, named = rm.value_masks(value)
    start = time.perf_counter()
    masked, _spans = named.mask_and_spans("Abcd1234.txt")
    assert time.perf_counter() - start < 1.0
    assert "Abcd1234" not in masked


def test_a_secret_after_many_repeats_of_its_probe_is_masked_whole():
    """Round-2 audit SF-2: 300 earlier occurrences of a piece of the secret
    never stop the search before the secret itself, so the name and a tag mask
    it whole."""
    strict, named = rm.value_masks("Fake123 " * 300 + "Demo@Fake123")
    assert named.mask("Demo@Fake123 router") == "[held] router"
    assert strict.mask("Demo@Fake123") == "[held]"
