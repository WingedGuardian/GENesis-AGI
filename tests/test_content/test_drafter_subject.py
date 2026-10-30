"""clean_header: an email subject as a safe one-line header value."""

from __future__ import annotations

import pytest


@pytest.mark.parametrize(("raw", "expected"), [
    ("Invoice\x00 ready\x1b[31m", "Invoice ready [31m"),
    ("Pay \u202eNOW\u202c please", "Pay NOW please"),
    ("Line\u2028break", "Line break"),
    ("x" * 300, "x" * 200),
])
def test_clean_header_removes_non_printing_characters_and_caps_length(raw, expected):
    from genesis.content.drafter import clean_header

    assert clean_header(raw) == expected
