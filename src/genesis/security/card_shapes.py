"""Payment card number detection: brand prefix, length and Luhn.

One detector for every place that must keep a card number away from the model,
logs and storage: browser tool guards, the reference store, reference
extraction and the outbound scanner. Stdlib only, with no ``genesis`` imports,
so a script can load this file by path (``scripts/browser.py`` does).

``scripts/hooks/secret_scrub.py`` names three credential-pattern files and asks
that a shape change be decided for each. For card numbers: ``output_scanner``
FLAGS one as critical and ``reference_extraction`` REFUSES to capture one. The
stdlib-only hook scrubber cannot import this module; its redacting copy of the
shape is tracked in issue #3172. Change the rules here and decide there too.

What counts as a card number:

* ASCII digits only. Python's ``\\d`` also matches other scripts' digits.
* Digit tokens are whole: an alphanumeric boundary on both sides, so digits
  inside a hex hash or an identifier never match. The groups of a canonical
  UUID are skipped too: its last group can be 12 decimal digits and its middle
  groups 4-4-4, both Maestro shapes.
* Either one unbroken 12-19 digit token, or consecutive tokens joined by one
  consistent separator (a space, a hyphen, or a no-break space) whose sizes form a standard printed
  grouping (4-4-4-4-3, 4-4-4-4, 4-6-5, 4-6-4, and 4-4-4 for a 12-digit
  Maestro). Every window of such a chain is
  tried, so a card followed by its expiry or security code ("4242 4242 4242
  4242 123") is still found; a longer reading that fails falls back to a
  shorter one.
* A chain of more than ``_MAX_CHAIN_GROUPS`` tokens is a table of numbers, not
  a card, and is skipped; so is a 4-4-4-4 window with another 4-digit token
  beside it that is not a plausible MMYY expiry (a list of 4-digit numbers,
  such as PR numbers in a shell loop). Both rules come from measurement: a
  spaced list of four or more 4-digit numbers in a shell command was the main
  false-positive class once neighbouring numbers stopped hiding real cards.
* Not a decimal fraction: a token right after ``<digit>.`` is ignored. In a
  30-day measurement over real transcripts, decimal fractions were the only
  false-positive class found.
* A known brand prefix with that brand's length (``_brand``), then Luhn.

Known limits, by design: other brands and lengths, Visa's 13-digit form (it
collides with phone numbers), and numbers split by anything other than one
space, hyphen or no-break space (tabs, dashes, line breaks) are not detected.
The brand table is a closed set on purpose: MEASURED over 333,530 real
transcript and memory items, dropping it for "any 13-19 digits that pass Luhn"
raised distinct non-card hits from 5 to 798, mostly 14-digit timestamps and
13-digit epoch milliseconds. Card entry goes through the vault, so nobody needs
to type a card number anywhere this detector watches.
"""

from __future__ import annotations

import re

REDACTED = "[card redacted]"

# Standard printed groupings of a card number.
# 4-4-4 is a 12-digit Maestro card; it inherits the 4-digit-neighbour rule below.
_GROUPINGS: tuple[tuple[int, ...], ...] = (
    (4, 4, 4, 4, 3),
    (4, 4, 4, 4),
    (4, 6, 5),
    (4, 6, 4),
    (4, 4, 4),
)
# A card plus its expiry and security code is at most 8 tokens ("4-4-4-4 MM YY
# CVV" is 7); a longer chain is a column of numbers, not a card.
_MAX_CHAIN_GROUPS = 8

# A space, a hyphen, or a no-break space (U+00A0, U+202F): web pages and
# statements print card groups with no-break spaces, and a copy keeps them.
_SEPARATORS = " -\u00a0\u202f"

_TOKEN = re.compile(r"(?<![A-Za-z0-9])[0-9]+(?![A-Za-z0-9])")

# A canonical UUID (8-4-4-4-12 hex). Its last group, or its middle groups, can
# be all decimal digits and pass the Maestro rules (12 digits, or 4-4-4), so its
# digits are never card tokens. No card is printed in that 32-digit layout, so
# the mask needs no left boundary: a UUID glued to a prefix is masked too.
_UUID = re.compile(
    r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}"
    r"(?![A-Za-z0-9])"
)


def luhn_ok(digits: str) -> bool:
    """True when ``digits`` (ASCII digits only) passes the Luhn checksum."""
    if not digits or not digits.isascii() or not digits.isdigit():
        return False
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = ord(ch) - 48
        if i % 2:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def _brand(digits: str) -> str | None:
    """The card brand whose prefix AND length ``digits`` match, else None."""
    length = len(digits)
    p2, p3, p4 = int(digits[:2]), int(digits[:3]), int(digits[:4])
    if digits[0] == "4" and length in (16, 19):
        return "visa"
    if (51 <= p2 <= 55 or 2221 <= p4 <= 2720) and length == 16:
        return "mastercard"
    if p2 in (34, 37) and length == 15:
        return "amex"
    # Diners/Discover Global Network IIN summary: 3000-3059 and 3800-3999 at
    # 16-19 digits, 3600-3699 at 14-19; the classic 14-digit form is kept.
    if (300 <= p3 <= 305 or p2 in (36, 38, 39)) and 14 <= length <= 19:
        return "diners"
    if (p4 == 6011 or 644 <= p3 <= 649 or p2 == 65) and 16 <= length <= 19:
        return "discover"
    if 3528 <= p4 <= 3589 and 16 <= length <= 19:
        return "jcb"
    if p2 == 62 and 16 <= length <= 19:
        return "unionpay"
    # Maestro also holds 6304 and 6759/6761-6763 (published IIN tables) and the
    # BIN blocks 63900000-63909999 and 67000000-67999999 (Mastercard rules), at
    # 12-19 digits (Mastercard's security rules).
    if (p2 in (50, 67) or 56 <= p2 <= 58 or p4 in (6304, 6390)) and 12 <= length <= 19:
        return "maestro"
    return None


def card_brand(digits: str) -> str | None:
    """The brand of a card number (digits only), or None if it is not one."""
    if not luhn_ok(digits) or len(digits) < 12:
        return None
    return _brand(digits)


def _tokens(text: str) -> list[tuple[int, int]]:
    """Whole digit tokens, minus decimal fractions ("0.4242…"), UUID groups and "+" numbers."""
    uuids = [m.span() for m in _UUID.finditer(text)]
    out = []
    for match in _TOKEN.finditer(text):
        start, end = match.span()
        if start >= 2 and text[start - 1] == "." and text[start - 2].isdigit():
            continue
        if any(a <= start < b for a, b in uuids):
            continue
        if start and text[start - 1] == "+":  # a phone number ("+57 3…"); no card has one
            continue
        out.append((start, end))
    return out


def _chains(text: str, tokens: list[tuple[int, int]]) -> list[list[tuple[int, int]]]:
    """Maximal runs of tokens joined by one consistent single-character separator.

    A token where the separator changes ends one chain AND starts the next:
    in "1230 4242-4242-4242-4242" the first card group belongs to both the
    space chain and the hyphen chain, or the card's own chain would start one
    group short.
    """
    chains: list[list[tuple[int, int]]] = []
    current: list[tuple[int, int]] = []
    sep: str | None = None
    for token in tokens:
        if current:
            gap = text[current[-1][1] : token[0]]
            if len(gap) == 1 and gap in _SEPARATORS:
                if sep is None or gap == sep:
                    sep = gap
                    current.append(token)
                    continue
                chains.append(current)
                current, sep = [current[-1], token], gap
                continue
            chains.append(current)
        current, sep = [token], None
    if current:
        chains.append(current)
    return chains


def _is_listed_number(text: str, token: tuple[int, int]) -> bool:
    """A 4-digit neighbour that is not a plausible MMYY expiry (month 01-12)."""
    start, end = token
    return end - start == 4 and not 1 <= int(text[start : start + 2]) <= 12


def find_card_numbers(text: str) -> list[tuple[int, int]]:
    """Spans ``(start, end)`` of every card number in ``text``."""
    if not text:
        return []
    candidates: list[tuple[int, int]] = []
    tokens = _tokens(text)
    for start, end in tokens:
        if 12 <= end - start <= 19 and card_brand(text[start:end]):
            candidates.append((start, end))
    for chain in _chains(text, tokens):
        if len(chain) < 3 or len(chain) > _MAX_CHAIN_GROUPS:
            continue
        sizes = [end - start for start, end in chain]
        for i in range(len(chain)):
            for grouping in _GROUPINGS:
                j = i + len(grouping)
                if tuple(sizes[i:j]) != grouping:
                    continue
                # A 4-4-4-4(-3) window next to another 4-digit token is a list
                # of 4-digit numbers ("for n in 2404 2424 2413 2445 2472"),
                # not a card: MEASURED as the dominant false-positive class.
                # Unless that neighbour reads as an MMYY expiry ("… 4242 1225"),
                # the most ordinary thing to paste beside a card. Expiry with a
                # slash, CVV (3 digits) and a 4-6-x layout's 4-digit code are
                # unaffected.
                if grouping[0] == 4 and grouping[1] == 4 and (
                    (i > 0 and _is_listed_number(text, chain[i - 1]))
                    or (j < len(chain) and _is_listed_number(text, chain[j]))
                ):
                    continue
                digits = "".join(text[a:b] for a, b in chain[i:j])
                if card_brand(digits):
                    candidates.append((chain[i][0], chain[j - 1][1]))
    # Longest first at each position; drop anything overlapping a kept span.
    spans: list[tuple[int, int]] = []
    for start, end in sorted(candidates, key=lambda span: (span[0], span[0] - span[1])):
        if spans and start < spans[-1][1]:
            continue
        spans.append((start, end))
    return spans


def contains_card_number(text: str) -> bool:
    """True when ``text`` holds at least one card number."""
    return bool(find_card_numbers(text))


def redact(text: str) -> str:
    """``text`` with every card number replaced by ``REDACTED``."""
    spans = find_card_numbers(text)
    if not spans:
        return text
    out, last = [], 0
    for start, end in spans:
        out.append(text[last:start])
        out.append(REDACTED)
        last = end
    out.append(text[last:])
    return "".join(out)
