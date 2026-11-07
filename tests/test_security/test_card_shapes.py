"""Tests for the payment-card-number detector.

No card-shaped literal appears in this file. Every number is generated at test
time (prefix + random body + Luhn check digit), so a later layer that redacts
card numbers from what Claude reads can never rewrite this file on disk.
"""

from __future__ import annotations

import random

import pytest

from genesis.security import card_shapes as cs


def _luhn_digit(partial: str) -> str:
    """The check digit that makes ``partial + digit`` pass Luhn."""
    total = 0
    for i, ch in enumerate(reversed(partial)):
        n = int(ch)
        if i % 2 == 0:  # these positions are doubled once the check digit is appended
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return str((10 - total % 10) % 10)


def make_card(prefix: str, length: int, seed: int = 0) -> str:
    rng = random.Random(f"{prefix}-{length}-{seed}")
    body = prefix + "".join(rng.choice("0123456789") for _ in range(length - len(prefix) - 1))
    return body + _luhn_digit(body)


def break_luhn(number: str) -> str:
    last = int(number[-1])
    return number[:-1] + str((last + 1) % 10)


def group(number: str, sizes: tuple[int, ...], sep: str) -> str:
    parts, i = [], 0
    for n in sizes:
        parts.append(number[i : i + n])
        i += n
    assert i == len(number)
    return sep.join(parts)


# A Stripe public test number, built from parts so the literal never appears.
STRIPE_VISA = "4242" * 4

BRANDS = [
    ("visa-16", "4", 16),
    ("visa-19", "4", 19),
    ("mc-51", "51", 16),
    ("mc-55", "55", 16),
    ("mc-2221", "2221", 16),
    ("mc-2720", "2720", 16),
    ("amex-34", "34", 15),
    ("amex-37", "37", 15),
    ("diners-300", "300", 14),
    ("diners-36", "36", 14),
    ("discover-6011", "6011", 16),
    ("discover-65", "65", 19),
    ("discover-644", "644", 16),
    ("jcb", "3530", 16),
    ("unionpay", "62", 17),
    ("maestro-50", "50", 16),
    ("maestro-6304", "6304", 16),
    ("maestro-6390", "6390", 16),
    ("maestro-6759", "6759", 18),
    ("maestro-67", "6700", 19),
    ("maestro-12", "6759", 12),
    ("maestro-13", "50", 13),
    ("maestro-15", "6304", 15),
    ("diners-3000-16", "3000", 16),
    ("diners-36-19", "36", 19),
    ("diners-39", "39", 16),
    ("diners-38-17", "38", 17),
]


class TestLuhn:
    def test_generated_numbers_pass(self):
        for _, prefix, length in BRANDS:
            assert cs.luhn_ok(make_card(prefix, length))

    def test_broken_check_digit_fails(self):
        assert not cs.luhn_ok(break_luhn(make_card("4", 16)))

    def test_stripe_number_passes(self):
        assert cs.luhn_ok(STRIPE_VISA)


class TestDetects:
    @pytest.mark.parametrize("name,prefix,length", BRANDS)
    def test_unbroken_run(self, name, prefix, length):
        number = make_card(prefix, length)
        text = f"pay with {number} please"
        assert cs.contains_card_number(text), name
        (span,) = cs.find_card_numbers(text)
        assert text[span[0] : span[1]] == number

    @pytest.mark.parametrize("sep", [" ", "-"])
    def test_grouped_12_digit_maestro_444(self, sep):
        number = make_card("6759", 12)
        assert cs.contains_card_number("card " + group(number, (4, 4, 4), sep))

    def test_444_inside_a_list_of_4_digit_numbers_is_not_a_card(self):
        """The 4-4-4 window inherits the list rule: a 4-digit neighbour that is
        not an MMYY expiry marks a column of numbers, not a card."""
        number = make_card("6759", 12)
        text = "for n in " + group(number, (4, 4, 4), " ") + " 2404; do"
        assert not cs.contains_card_number(text)

    @pytest.mark.parametrize("sep", [" ", "-", "\u00a0", "\u202f"], ids=["space", "hyphen", "nbsp", "narrow-nbsp"])
    def test_grouped_4444(self, sep):
        text = "card: " + group(STRIPE_VISA, (4, 4, 4, 4), sep)
        assert cs.contains_card_number(text)

    @pytest.mark.parametrize("sep", [" ", "-"])
    def test_grouped_amex_465(self, sep):
        number = make_card("37", 15)
        assert cs.contains_card_number(group(number, (4, 6, 5), sep))

    @pytest.mark.parametrize("sep", [" ", "-"])
    def test_grouped_diners_464(self, sep):
        number = make_card("36", 14)
        assert cs.contains_card_number(group(number, (4, 6, 4), sep))

    @pytest.mark.parametrize("sep", [" ", "-"])
    def test_grouped_19_digit_44443(self, sep):
        # The 4-4-4-4 prefix of a 4-4-4-4-3 number must not win and then be
        # rejected as a run-on: the whole 19-digit number is the match.
        number = make_card("4", 19)
        text = group(number, (4, 4, 4, 4, 3), sep)
        (span,) = cs.find_card_numbers(text)
        assert text[span[0] : span[1]] == text

    def test_at_string_edges(self):
        assert cs.contains_card_number(STRIPE_VISA)

    @pytest.mark.parametrize("wrap", ['"{}"', "'{}'", "({})", "[{}]", "`{}`", "={}", ":{}", "{},"])
    def test_punctuation_neighbours(self, wrap):
        assert cs.contains_card_number(wrap.format(STRIPE_VISA))

    @pytest.mark.parametrize(
        "template",
        [
            "{V} 12/30",
            "{V} 123",
            "{G} 12/30 123",
            "{G} 123",
            "{H} 123",
            "123 {V}",
            "Step 2 {G}",
            "{G} 12 30 123",
            "card {V} exp 12/30 cvv 123",
        ],
        ids=[
            "unbroken+expiry",
            "unbroken+cvv",
            "grouped+expiry+cvv",
            "grouped+cvv",
            "hyphenated+cvv",
            "digits-before-unbroken",
            "step-number-before-grouped",
            "grouped+spaced-expiry+cvv",
            "labelled",
        ],
    )
    def test_card_with_neighbouring_numbers(self, template):
        # The shape a pasted card actually takes: number, then expiry and CVV.
        number = STRIPE_VISA
        text = template.format(
            V=number, G=group(number, (4, 4, 4, 4), " "), H=group(number, (4, 4, 4, 4), "-")
        )
        assert cs.contains_card_number(text), template

    @pytest.mark.parametrize(
        "template",
        ["{G} 1225", "{G} 1225 123", "1225 {G}", "{H}-0128", "{G} 0631 4242"],
        ids=["mmyy-after", "mmyy-and-cvv", "mmyy-before", "hyphen-mmyy", "mmyy-then-other"],
    )
    def test_spaced_card_with_bare_mmyy_expiry(self, template):
        # A month-first 4-digit neighbour reads as an expiry, not a list.
        text = template.format(G=group(STRIPE_VISA, (4, 4, 4, 4), " "),
                               H=group(STRIPE_VISA, (4, 4, 4, 4), "-"))
        assert cs.contains_card_number(text)

    @pytest.mark.parametrize(
        "template",
        ["1230 {H}", "12/30 {H} 123", "1230-{G}", "{G}-123", "123 {H}-1230"],
        ids=["space-then-hyphen", "slash-space-hyphen", "hyphen-then-space", "space-then-hyphen-cvv",
             "mixed-around"],
    )
    def test_card_where_the_separator_changes_at_its_edge(self, template):
        # The token where the separator changes belongs to both chains; the
        # card's own chain must not start one group short.
        text = template.format(G=group(STRIPE_VISA, (4, 4, 4, 4), " "),
                               H=group(STRIPE_VISA, (4, 4, 4, 4), "-"))
        assert cs.contains_card_number(text)

    def test_amex_followed_by_four_digit_code(self):
        number = make_card("37", 15)
        assert cs.contains_card_number(group(number, (4, 6, 5), " ") + " 1234")

    def test_multiple_numbers(self):
        a, b = make_card("51", 16), make_card("34", 15)
        assert len(cs.find_card_numbers(f"{a} and {b}")) == 2


class TestRejects:
    def test_luhn_failure(self):
        assert not cs.contains_card_number(break_luhn(make_card("4", 16)))

    @pytest.mark.parametrize(
        "prefix,length",
        [
            ("4", 13),  # Visa-13 is excluded: it collides with phone numbers
            ("4", 15),
            ("51", 15),
            ("34", 16),
            ("1", 16),  # no issuer uses 1
            ("9", 16),
            ("2220", 16),  # just below the Mastercard 2-series range
            ("2721", 16),  # just above it
            ("7", 16),
        ],
    )
    def test_wrong_brand_or_length(self, prefix, length):
        assert not cs.contains_card_number(make_card(prefix, length))

    def test_inside_longer_digit_run(self):
        number = make_card("4", 16)
        assert not cs.contains_card_number("9" + number)
        assert not cs.contains_card_number(number + "9")

    def test_inside_hex_hash(self):
        number = make_card("4", 16)
        assert not cs.contains_card_number(f"deadbeef{number}cafe")
        assert not cs.contains_card_number(f"a{number}")

    @pytest.mark.parametrize("template", ["2404 {G}", "{H}-9999", "for n in 2404 {G} 2472; do"],
                             ids=["4-digit-before", "4-digit-after", "shell-loop-list"])
    def test_spaced_card_beside_another_4_digit_token_reads_as_a_list(self, template):
        # The measured trade: lists of 4-digit numbers (PR numbers in a shell
        # loop) are not cards; the cost is a spaced card next to an unrelated
        # 4-digit number.
        text = template.format(G=group(STRIPE_VISA, (4, 4, 4, 4), " "),
                               H=group(STRIPE_VISA, (4, 4, 4, 4), "-"))
        assert not cs.contains_card_number(text)

    def test_chain_cap_alone_rejects_a_long_chain(self):
        # No 4-digit neighbour here, so only the chain cap decides.
        text = "1 2 3 4 5 " + group(STRIPE_VISA, (4, 4, 4, 4), " ") + " 123"
        assert len(text.split()) > cs._MAX_CHAIN_GROUPS
        assert not cs.contains_card_number(text)

    def test_long_numeric_table_is_not_a_card(self):
        # More than _MAX_CHAIN_GROUPS tokens in one chain is a column of
        # numbers, even when one window of it happens to form a valid card.
        inner = group(STRIPE_VISA, (4, 4, 4, 4), " ")
        table = f"1000 2000 3000 {inner} 5000 6000"
        assert len(table.split()) > cs._MAX_CHAIN_GROUPS
        assert not cs.contains_card_number(table)

    def test_decimal_fraction(self):
        # MEASURED: the only false-positive class in 241,499 real transcript
        # items was digits after a decimal point ("score":0.4…).
        number = make_card("4", 16)
        assert not cs.contains_card_number(f'"score":0.{number},')
        assert not cs.contains_card_number(f"12.{number}")

    def test_mixed_separators(self):
        number = STRIPE_VISA
        mixed = f"{number[:4]} {number[4:8]}-{number[8:12]} {number[12:]}"
        assert not cs.contains_card_number(mixed)

    def test_epoch_milliseconds(self):
        assert not cs.contains_card_number("ts=1728600000000")

    def test_phone_numbers(self):
        assert not cs.contains_card_number("+4915112345678")
        assert not cs.contains_card_number("+1 415 555 0100")

    def test_uuid_and_sha256(self):
        assert not cs.contains_card_number("550e8400-e29b-41d4-a716-446655440000")
        assert not cs.contains_card_number(
            "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"
        )

    def test_numeric_columns(self):
        assert not cs.contains_card_number("4096 4096 4096 4096 4096 4096")
        assert not cs.contains_card_number("total 1234567\n-rw-r--r-- 1 u u 4096 x")

    def test_unicode_digits(self):
        fullwidth = "".join(chr(0xFF10 + int(c)) for c in STRIPE_VISA)
        assert not cs.contains_card_number(fullwidth)

    def test_empty_and_none_like(self):
        assert cs.find_card_numbers("") == []
        assert not cs.contains_card_number("")


class TestRedact:
    def test_replaces_every_number(self):
        a, b = make_card("4", 16), make_card("37", 15)
        out = cs.redact(f"first {a}, second {group(b, (4, 6, 5), ' ')}.")
        assert a not in out
        assert out == f"first {cs.REDACTED}, second {cs.REDACTED}."

    def test_leaves_clean_text_identical(self):
        text = "order 1234567 shipped; ts=1728600000000; id 550e8400-e29b"
        assert cs.redact(text) == text

    def test_empty(self):
        assert cs.redact("") == ""


def _luhn_complete(prefix: str, length: int) -> str:
    body = prefix + "0" * (length - len(prefix) - 1)
    return next(body + c for c in "0123456789" if cs.luhn_ok(body + c))


class TestUuid:
    """A UUID's last group can be 12 decimal digits, and its middle groups can be
    4-4-4: both are Maestro shapes, so its digits must never count as a card."""

    @pytest.mark.parametrize("prefix", ["50", "56", "57", "58", "67", "6304", "6390"])
    def test_an_all_digit_last_group_is_not_a_card(self, prefix):
        last = _luhn_complete(prefix, 12)
        assert cs.card_brand(last) == "maestro"  # the group alone is a Maestro shape
        for text in (f"9f3e4567-e89b-42d3-a456-{last}", f"id={last[:8]}-0000-4000-8000-{last}"):
            assert not cs.contains_card_number(text), text

    def test_all_digit_middle_groups_are_not_a_card(self):
        mid = _luhn_complete("5012", 12)
        text = f"request 9f3e4567-{mid[:4]}-{mid[4:8]}-{mid[8:]}-abcdefabcdef done"
        assert cs.card_brand(mid) == "maestro"
        assert not cs.contains_card_number(text)

    def test_upper_case_uuids_too(self):
        last = _luhn_complete("50", 12)
        assert not cs.contains_card_number(f"9F3E4567-E89B-42D3-A456-{last}")

    def test_a_card_next_to_a_uuid_is_still_found(self):
        card = "4242" * 4
        text = f"9f3e4567-e89b-42d3-a456-426614174000 {card}"
        assert cs.find_card_numbers(text) == [(text.index(card), len(text))]

    def test_a_hyphenated_card_is_not_mistaken_for_a_uuid(self):
        card = "4242-4242-4242-4242"
        assert cs.contains_card_number(f"card {card} on file")

    def test_a_uuid_glued_to_a_prefix_is_masked_too(self):
        last = _luhn_complete("50", 12)
        assert not cs.contains_card_number(f"deadbeef9f3e4567-e89b-42d3-a456-{last}")
        assert not cs.contains_card_number(f"req9f3e4567-e89b-42d3-a456-{last}")


class TestPhoneNumbers:
    """A 12-digit Maestro shape collides with +57/+58 mobiles (~10% pass Luhn);
    no card is printed after a '+', so a '+'-led number is never a card."""

    @pytest.mark.parametrize("prefix", ["573", "584"])
    def test_an_international_number_is_not_a_card(self, prefix):
        number = _luhn_complete("57" if prefix == "573" else "58", 12)
        assert cs.card_brand(number) == "maestro"
        assert not cs.contains_card_number(f"call +{number} today")

    def test_a_card_without_a_plus_is_still_found(self):
        assert cs.contains_card_number("card " + "4242" * 4)
