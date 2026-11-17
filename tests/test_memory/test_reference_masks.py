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


# ─── merging probes from several sources ─────────────────────────────────────


def test_a_login_is_exempt_only_when_every_source_has_it_as_a_login():
    """The current password equals the old entry's login: the identifier still
    masks it, because one source requires it as a secret."""
    _strict, named = rm.value_masks("newuser / admin9x", also=(("admin9x / OldPass7!", None),))
    assert named.mask("router admin9x") == "router [held]"
    _strict, named = rm.value_masks("admin9x / Pw9fake!x", also=(("admin9x / OldPass7!", None),))
    assert named.mask("admin9x router") == "admin9x router"


def test_a_probe_given_both_unbounded_and_whole_word_stays_unbounded():
    """A captured "pin: 4821" (whole word) never weakens the value's own
    unbounded probe of the same digits."""
    assert rm.value_masks("4821")[0].mask("router4821") == "router[held]"
    assert rm.value_masks("4821", detail="pin: 4821")[0].mask("router4821") == "router[held]"


def _within(seconds: float, call):
    """``call()`` run in a daemon thread: fails, rather than hangs, when it does
    not return in ``seconds``."""
    import threading

    out: list = []
    worker = threading.Thread(target=lambda: out.append(call()), daemon=True)
    worker.start()
    worker.join(seconds)
    assert not worker.is_alive(), "masking did not terminate"
    return out[0]


def test_an_empty_normalised_probe_is_dropped():
    """A captured secret of zero-width characters normalises to nothing: it is
    no probe, and masking terminates."""
    strict, _named = rm.value_masks("password: " + "​" * 4, prose=True)
    assert _within(5, lambda: strict.mask("-")) == "-"


def test_an_old_value_grows_a_match_over_its_own_runs():
    """A held old URL's password ``Demo@Fake123`` is masked whole in a name,
    not only its secret-shaped fragment: runs come from every secret source."""
    old = "https://admin:Demo@Fake123@example.org"
    strict, named = rm.value_masks("NewPw9!x", also=((old, None),))
    assert named.mask("pw Demo@Fake123 y") == "pw [held] y"
    assert strict.mask("pw Demo@Fake123 y") == "pw [held] y"


def test_a_value_spelling_the_mask_text_is_masked():
    """A value with ``[held]`` literally inside is masked where a tag spells it;
    text around a REAL mask of a value without it still never joins."""
    strict, _named = rm.value_masks("orange[held]banana")
    assert strict.mask("tag orange[held]banana") == "tag [held]"
    assert rm._mask_tag(strict, "orange[held]banana") is None
    strict, _named = rm.value_masks("abcd1234")
    assert not strict.carries("abcd[held]1234")
    assert strict.mask("abcd[held]1234") == "abcd[held]1234"


# ─── a secret part is always a probe (round-2 class audit) ───────────────────
#
# The secret half of "login / secret", the password of a URL's userinfo and a
# captured secret as captured are probes as written, whatever their shape; the
# skips of a fragment (a host, a scheme, a port) apply only where it sits.

_SECRET_FORMS = {
    "plain word": "hunter",
    "all digits": "48291375",
    "dotted": "Demo.Fakeword",
    "punctuated": "Fakeword!",
    "mixed": "Fk9@x!Z2",
}
_POSITIONS = {
    "login / secret": lambda s: (f"bob / {s}", None),
    "url userinfo": lambda s: (f"postgres://app:{s}@db.example.com/prod", None),
    "captured label": lambda s: ("see the note", f"password: {s}"),
    "plain delimited": lambda s: (f"front door {s}", None),
}
#: Stated limits: a plain word inside a plain delimited value is matched whole
#: only, a plain-word URL password stays visible, and a dotted fragment of a
#: plain delimited value reads as a host name, which is not probed (owner
#: ruling 2026-11-17: the fragment rules stay the shape rules).
_STATED_LIMITS = {
    ("plain delimited", "plain word"),
    ("url userinfo", "plain word"),
    ("plain delimited", "dotted"),
}


@pytest.mark.parametrize("form", list(_SECRET_FORMS))
@pytest.mark.parametrize("position", list(_POSITIONS))
def test_no_masked_field_carries_a_secret_part(position, form):
    if (position, form) in _STATED_LIMITS:
        pytest.skip("stated limit for a plain-word secret in this position")
    secret = _SECRET_FORMS[form]
    value, detail = _POSITIONS[position](secret)
    strict, named = rm.value_masks(value, detail=detail)
    for mask in (strict, named):
        for field in (f"box {secret} end", secret, f"{secret}, other"):
            masked = mask.mask(field)
            assert secret not in masked and not mask.carries(masked), (field, masked)


@pytest.mark.parametrize(
    ("value", "field", "masked"),
    [
        ("bob / hunter", "bob hunter box", "bob [held] box"),
        ("bob / 4821", "bob 4821", "bob [held]"),
        ("admin / admin", "admin router", "[held] router"),
        # A plain-word URL password is the stated limit (owner ruling 2026-11-17).
        ("postgres://app:hunter@db.example.com/prod", "prod hunter db", "prod hunter db"),
    ],
)
def test_a_plain_secret_part_masks_in_the_identifier_and_a_tag(value, field, masked):
    strict, named = rm.value_masks(value)
    assert named.mask(field) == masked
    assert strict.mask(field) == masked


@pytest.mark.parametrize(
    ("value", "field"),
    [("admin9x / admin9x", "admin9x"), ("admin9x / admin9x!", "admin9x!")],
)
def test_a1_a_secret_half_equal_to_its_login_is_masked(value, field):
    strict, named = rm.value_masks(value)
    assert named.mask(field) == "[held]"
    assert strict.mask(field) == "[held]"
    assert rm.mask_value_spans(value, field, login_exempt=True) == "[held]"
    _strict, named = rm.value_masks("NewPw9!x", also=(("other9x / other9x", None),))
    assert named.mask("other9x") == "[held]"


@pytest.mark.parametrize(
    ("value", "field"),
    [
        ("admin / Fakeword!", "Fakeword!"),
        ("admin / Ab1!", "Ab1!"),
        ("admin / Ab1?", "Ab1?"),
        ("admin / Ab1'", "Ab1'"),
        ("postgres://u:Fakeword!@h.example.org", "Fakeword!"),
        ("token=Ab1!&x=1", "Ab1!"),
    ],
)
def test_a2_edge_punctuation_never_unprobes_a_secret(value, field):
    strict, named = rm.value_masks(value)
    assert named.mask(field) == "[held]"
    assert strict.mask(field) == "[held]"


@pytest.mark.parametrize(
    ("value", "field", "masked"),
    [
        ("admin / Demo.Fakeword", "Demo.Fakeword", "[held]"),
        ("admin / Fake9.zzz", "Fake9.zzz", "[held]"),
        ("postgres://svc:Fake9.zzz@db.example.org/x", "Fake9.zzz", "[held]"),
        ("postgres://svc:Fake9.zzz@db.example.org/x", "prefixFake9.zzz", "prefix[held]"),
        # A dotted fragment of a plain delimited value reads as a host name
        # and is not probed: the fragment rules stay the shape rules (owner
        # ruling 2026-11-17).
        pytest.param(
            "token=abc1.defg", "abc1.defg", "[held]",
            marks=pytest.mark.skip(reason="stated limit: a host-shaped fragment is not probed"),
        ),
    ],
)
def test_a3_a_dotted_secret_is_no_host_name(value, field, masked):
    strict, named = rm.value_masks(value)
    assert named.mask(field) == masked
    assert strict.mask(field) == masked


def test_a3_a_host_and_port_in_their_position_stay_visible():
    _strict, named = rm.value_masks("redis://:Rz9fakeKey77@cache.example.org:6379/0")
    assert named.mask("cache.example.org:6379 redis") == "cache.example.org:6379 redis"
    assert named.mask("Rz9fakeKey77") == "[held]"


@pytest.mark.parametrize("edge", [".", ":", ")", '"', "'"])
def test_a4_a_captured_secret_is_probed_as_captured(edge):
    secret = "Fakeword" + edge
    assert {secret, "Fakeword"} <= captured_secrets([f"password: {secret}"])
    strict, named = rm.value_masks("see note", detail=f"password: {secret}")
    assert named.mask(f"prefix{secret}") == "prefix[held]"
    assert strict.mask(f"prefix{secret}") == "prefix[held]"
    _strict, named = rm.value_masks(f"router (password: {secret}", prose=True)
    assert named.mask(f"prefix{secret}") == "prefix[held]"


@pytest.mark.parametrize("count", [1, 63, 64, 65, 300])
def test_a5_every_run_grows_whole_in_any_order(count):
    runs = ["a" * i + "@Fake123" for i in range(1, count + 1)] + ["Demo@Fake123"]
    sample = {runs[0], runs[len(runs) // 2], runs[-2], runs[-1]}
    for order in (runs, runs[::-1]):
        _strict, named = rm.value_masks(" ".join(order))
        for run in sample:
            assert named.mask(f"{run} router") == "[held] router", run


def test_a5_a_run_longer_than_the_old_cap_grows_whole():
    strict, _named = rm.value_masks("Pw9" + "x" * 300 + "@Fake123 z")
    assert strict.mask("x" * 200 + "@Fake123") == "[held]"


@pytest.mark.parametrize(
    ("value", "field", "masked"),
    [
        ("admin / 48291375", "48291375", "[held]"),
        ("postgres://u:48291375@h.example.org/db", "48291375", "[held]"),
        ("front door code 4821", "door 4821", "door [held]"),
        ("front door pin 4821", "door 4821", "door [held]"),
        # A whole number: another number holding it is no match.
        ("admin / 48291375", "a482913750", "a482913750"),
    ],
)
def test_a6_an_all_digit_secret_is_masked_as_a_whole_number(value, field, masked):
    strict, named = rm.value_masks(value)
    assert named.mask(field) == masked
    assert strict.mask(field) == masked


@pytest.mark.parametrize("mark", [rm.SECRET_PLACEHOLDER, rm.DETAIL_PLACEHOLDER])
def test_a7_a_secret_spelling_a_placeholder_is_masked(mark):
    value = f"Fake{mark}9x"
    strict, named = rm.value_masks(value)
    assert strict.mask(value) == "[held]"
    assert named.mask(f"x {value} y") == "x [held] y"
    # The placeholder itself is still no leak of another value.
    assert not rm.value_masks("Zq8vLm2x")[0].carries(f"x {mark} y")


def test_a8_a_described_secret_grows_over_its_own_runs():
    _strict, named = rm.value_masks("other X9y8z7w6", detail="password: Demo@Fake123!")
    assert named.mask("Demo@Fake123") == "[held]"


# ─── the narrowed fragment rules (owner ruling 2026-11-17) ───────────────────


@pytest.mark.parametrize("password", ["Pass?word", "Pass/word", "Pass#word", "Pass word", "Xy#9zQ/w"])
@pytest.mark.parametrize("host", ["h", "host.example.org"])
def test_m1_a_password_with_an_unencoded_reserved_character_is_a_probe(password, host):
    value = f"postgres://bob:{password}@{host}/db"
    assert (rm._normalise(password), False) in rm._secret_parts(value)
    strict, named = rm.value_masks(value)
    for mask in (strict, named):
        assert mask.mask(f"x {password} y") == "x [held] y"


@pytest.mark.parametrize(
    "value",
    [
        "https://h.example.org:8443/path/me@x.example.org",
        "https://h.example.org/a:b9x/c@x.example.org",
        "https://h.example.org/x?q=a:b9x c@x.example.org",
        "https://bob@h.example.org/p:q9x/r@x.example.org",
    ],
)
def test_m1_an_at_sign_in_a_url_path_is_no_password(value):
    assert rm._secret_parts(value) == []


def test_m1_a_well_formed_userinfo_still_ends_at_its_last_at_sign():
    assert (rm._normalise("p@ss9x"), False) in rm._secret_parts("redis://bob:p@ss9x@h.example.org/0")


@pytest.mark.parametrize(
    ("value", "field", "masked"),
    [
        # M3: an IP is probed again, in a URL authority and a host:port.
        ("http://192.0.2.5:8080/admin", "Admin 192.0.2.5", "Admin [held]"),
        ("admin / 192.0.2.5:8080", "note 192.0.2.5", "note [held]"),
        ("ssh://root@192.0.2.5:22/ Zq9vLm2x", "tag 192.0.2.5", "tag [held]"),
        # M4: a host name, an email domain and a port are not probed.
        ("bob@example.com / pw9x!", "Gmail example.com work", "Gmail example.com work"),
        ("host=db.example.org port=5432 pass=Zx9kq7", "db.example.org staging", "db.example.org staging"),
        ("db.example.org:5432 Zx9kq7", "postgres 5432", "postgres 5432"),
        ("ssh://git@github.com/org/repo.git", "repo.git deploy key", "repo.git deploy key"),
        # A digit run that is not a port is a whole-word probe.
        ("front door code 4821", "door 4821", "door [held]"),
        ("db.example.org:5432 and pin 5432", "postgres 5432", "postgres [held]"),
    ],
)
def test_the_narrowed_fragment_rules(value, field, masked):
    strict, named = rm.value_masks(value)
    assert named.mask(field) == masked
    assert strict.mask(field) == masked


def test_m2_a_large_value_of_shared_probe_runs_masks_in_bounded_time():
    """Round-2 fix audit M2: a 2.6 MB held value whose 150,000 runs share one
    short probe, against a 16 KB field with 1,000 matches. The run cache is
    bounded, so the match masks its whole token instead (the uncapped index
    took about 6 s here)."""
    import random

    rng = random.Random(2)
    value = "Ab12 " + " ".join(f"x{i}Ab12y{i}" for i in range(150_000))
    assert len(value) > 2_500_000
    mask = rm.ValueMask(value)
    field = " ".join(
        f"Ab12{rng.choice('qwrtpsd')}{rng.randrange(10**6)}" for _ in range(1000)
    )[:16_000]
    started = time.perf_counter()
    out = mask.mask(field)
    assert time.perf_counter() - started < 3.0
    assert "ab12" not in out.casefold()
    assert not mask.carries(out)


@pytest.mark.parametrize("count", [64, 65])
def test_m2_a_probe_past_the_run_cache_masks_its_whole_token(count):
    runs = [f"{'a' * i}@Fake123" for i in range(1, count + 1)]
    _strict, named = rm.value_masks(" ".join(runs))
    assert named.mask("zz@Fake123q router") == ("[held] router" if count > 64 else "zz[held]q router")


def test_many_distinct_host_ports_build_masks_in_linear_time():
    """Round-2 audit H1: counting each port's whole-word occurrences took a
    pass over the whole value per distinct port (8,000 ports: ~92 s)."""
    value = "\n".join(f"db{i}.example.org:{10000 + i}" for i in range(8000)) + "\nadmin / Zq9!fake"
    started = time.monotonic()
    strict, named = rm.value_masks(value)
    assert time.monotonic() - started < 5.0
    assert strict.mask("x Zq9!fake y") == "x [held] y"
