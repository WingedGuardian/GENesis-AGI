"""card_redaction: the result walker, the tool decorator and the log filter.

Only Stripe's public test number appears here.
"""

from __future__ import annotations

import asyncio
import inspect
import logging

import pytest

from genesis.security import card_redaction
from genesis.security.card_redaction import (
    CARD_FILTER,
    CardRedactingFilter,
    contains_card_text,
    redact_text,
    redact_value,
    redacted,
)
from genesis.security.card_shapes import REDACTED

_CARD = "4242424242424242"
_MAX_SAFE = 2**53 - 1  # JavaScript's Number.MAX_SAFE_INTEGER


def test_module_imports_only_stdlib_and_card_shapes():
    import ast
    from pathlib import Path

    tree = ast.parse(Path(card_redaction.__file__).read_text())
    genesis_imports = {
        n.module
        for n in ast.walk(tree)
        if isinstance(n, ast.ImportFrom) and n.module and n.module.startswith("genesis")
    } | {
        a.name
        for n in ast.walk(tree)
        if isinstance(n, ast.Import)
        for a in n.names
        if a.name.startswith("genesis")
    }
    assert genesis_imports == {"genesis.security.card_shapes"}


class TestRedactValue:
    def test_strings_keys_numbers_and_nesting(self):
        got = redact_value(
            {
                _CARD: [f"pan {_CARD}", int(_CARD), float(_CARD), ("x", _CARD)],
                "count": 3,
                "flag": True,
                "none": None,
            }
        )
        assert got == {
            REDACTED: [f"pan {REDACTED}", REDACTED, REDACTED, ("x", REDACTED)],
            "count": 3,
            "flag": True,
            "none": None,
        }

    def test_json_text_with_escaped_digits(self):
        text = '{"pan": "\\u0034242424242424242", "n": 1}'
        out = redact_text(text)
        assert "4242" not in out and REDACTED in out
        assert '"n": 1' in out

    def test_a_json_string_scalar_is_decoded(self):
        out = redact_text('"\\u0034242424242424242"')
        assert out == f'"{REDACTED}"'

    def test_numbers_beyond_the_safe_integer_range_are_withheld(self):
        """A 17-19 digit card returned as a JS Number arrives rounded and fails
        the checksum; every number in that range is withheld."""
        big = int(_CARD + "123")  # 19 digits, built at runtime: not a card literal
        assert big > _MAX_SAFE
        assert redact_value(big) == REDACTED
        assert redact_value(float(big)) == REDACTED
        assert redact_value(-big) == REDACTED
        assert redact_value({"a": [1, {"b": big}]}) == {"a": [1, {"b": REDACTED}]}
        out = redact_text(f'{{"n": {big}, "m": 2}}')
        assert str(big) not in out and REDACTED in out and '"m": 2' in out

    def test_numbers_that_are_not_cards_stay(self):
        for n in (_MAX_SAFE, 1_760_000_000_000, 10**19, 12345, 0.5):
            assert redact_value(n) == n
        assert redact_value(float("inf")) == float("inf")

    def test_text_without_a_card_is_unchanged(self):
        for text in ("order 12345", '{"a": 1}', "[1, 2", "- heading: x", '"quoted" text', '"plain"'):
            assert redact_text(text) == text


class TestRedactedDecorator:
    @pytest.mark.asyncio
    async def test_async_result_and_exception(self):
        @redacted
        async def _impl_tool(fail: bool):
            if fail:
                raise RuntimeError(f"bad {_CARD}")
            return {"url": f"https://x.example/{_CARD}"}

        assert inspect.iscoroutinefunction(_impl_tool)
        assert await _impl_tool(False) == {"url": f"https://x.example/{REDACTED}"}
        err = await _impl_tool(True)
        assert err == {"error": f"tool failed: RuntimeError: bad {REDACTED}"}

    def test_sync_stays_sync(self):
        @redacted
        def _impl_tool():
            return {"v": _CARD}

        assert not inspect.iscoroutinefunction(_impl_tool)
        assert _impl_tool() == {"v": REDACTED}

    @pytest.mark.asyncio
    async def test_timeout_and_cancellation_still_propagate(self):
        @redacted
        async def _impl_x():
            raise TimeoutError(f"slow {_CARD}")

        with pytest.raises(TimeoutError) as err:
            await _impl_x()
        assert _CARD not in str(err.value)

        @redacted
        async def _impl_y():
            raise asyncio.CancelledError

        with pytest.raises(asyncio.CancelledError):
            await _impl_y()

    def test_a_result_too_deep_to_check_is_withheld(self):
        deep: list = []
        for _ in range(5000):
            deep = [deep]

        @redacted
        def _impl_z():
            return {"result": deep}

        assert "could not be checked" in _impl_z()["error"]


def _capture(target, handler_filter=None):
    records: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record):
            records.append(self.format(record))

    handler = _Capture()
    if handler_filter is not None:
        handler.addFilter(handler_filter)
    target.addHandler(handler)
    return records, handler


class TestFilter:
    def test_on_a_handler_it_redacts_propagated_records(self):
        parent = logging.getLogger("card_redaction_test")
        child = logging.getLogger("card_redaction_test.child")
        records, handler = _capture(parent, CardRedactingFilter())
        try:
            try:
                raise RuntimeError(f"boom {_CARD}")
            except RuntimeError:
                child.error("saw %s", _CARD, exc_info=True, stack_info=True)
        finally:
            parent.removeHandler(handler)
        assert len(records) == 1
        assert _CARD not in records[0]
        assert "Traceback" in records[0] and REDACTED in records[0]

    def test_the_decorator_failure_log_is_redacted(self):
        records, handler = _capture(card_redaction.logger)
        assert CARD_FILTER in card_redaction.logger.filters
        try:

            @redacted
            def _impl_tool():
                raise RuntimeError(f"bad {_CARD}")

            _impl_tool()
        finally:
            card_redaction.logger.removeHandler(handler)
        assert len(records) == 1
        assert _CARD not in records[0] and REDACTED in records[0]


class TestSplitAcrossStructure:
    """Card digits split across rendering or structure, which a per-scalar
    detector does not see."""

    @pytest.mark.parametrize(
        "text",
        [
            "Card\n4242\n4242\n4242\n4242\nPay",
            '- textbox "Card number": "4242"\n  - textbox "Card number": "4242"\n'
            '  - textbox "Card number": "4242"\n  - textbox "Card number": "4242"',
            "4242 | 4242 | 4242 | 4242",
            "4242  4242  4242  4242",
            "4242\t4242\t4242\t4242",
            "4 2 4 2 4 2 4 2 4 2 4 2 4 2 4 2",
            "[4][2][4][2][4][2][4][2][4][2][4][2][4][2][4][2]",
            # a checkout: quantity before, expiry, security code and ZIP after
            '- text: Qty 3\n  - textbox "Card": "4242"\n  - textbox "Card": "4242"\n'
            '  - textbox "Card": "4242"\n  - textbox "Card": "4242"\n'
            '  - textbox "MM": "05"\n  - textbox "YY": "29"\n  - textbox "CVC": "123"\n'
            '  - textbox "ZIP": "94110"',
        ],
    )
    def test_split_text_is_found_and_redacted(self, text):
        assert contains_card_text(text)
        out = redact_text(text)
        assert REDACTED in out
        assert not contains_card_text(out)

    @pytest.mark.parametrize(
        "text",
        [
            "Call +1 555 123 4567 or +1 555 987 6543",
            "Years 2019\n2020\n2021\n2022\n2023\n2024",
            "Order #100234567 total 12.50, qty 3",
            "Price 4.242 4242 4242 4242",
            "id 3f4a2b1c9d8e7f6a5b4c3d2e1f0a",
            '- textbox "Card number part 1": "4242"\n- textbox "Card number part 2": "4242"',
            # inside a list of 4-digit numbers: the detector's table rule
            "1999\n4242\n4242\n4242\n4242\n1888",
        ],
    )
    def test_non_cards_and_stated_limits_stay(self, text):
        assert redact_text(text) == text

    @pytest.mark.parametrize(
        "value",
        [
            ["4242", "4242", "4242", "4242"],
            ["x", "4242", "4242", "4242", "4242", 5],
            {"a": "4242", "b": "4242", "c": "4242", "d": "4242"},
            [["4242", "4242"], [{"z": "4242"}, "4242"]],
            list(_CARD),
            [_CARD[:8], _CARD[8:]],
            [4242, 4242, 4242, 4242],
        ],
    )
    def test_container_hiding_a_card_is_withheld(self, value):
        assert redact_value(value) == REDACTED

    @pytest.mark.parametrize(
        "value",
        [
            [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16],
            [2019, 2020, 2021, 2022, 2023, 2024],
            {"x": 120, "y": 340, "w": 800, "h": 600},
            ["10", "11", "12", "13", "14", "15", "16", "17", "18"],
        ],
    )
    def test_containers_of_ordinary_numbers_stay(self, value):
        assert redact_value(value) == value

    def test_colliding_keys_withhold_the_mapping(self):
        assert redact_value({_CARD: 1, REDACTED: 2}) == REDACTED
        assert redact_value({_CARD: 1, "4242 4242 4242 4242": 2}) == REDACTED
        assert redact_value({_CARD: 1, "other": 2}) == {REDACTED: 1, "other": 2}

    def test_a_withheld_dict_result_keeps_the_dict_shape(self):
        @redacted
        def _impl_tool():
            return {"a": "4242", "b": "4242", "c": "4242", "d": "4242"}

        assert "spelled a card number" in _impl_tool()["error"]


class TestSubrunsAndKeys:
    """Round 3: every contiguous subrun of digit strings, and mapping keys."""

    @pytest.mark.parametrize(
        "value",
        [
            # a trailing number joins the run; the card is a subrun of it
            {"p1": "42424242", "p2": "42424242", "count": 1},
            # unequal widths (7 + 9) inside a longer run of digit strings
            ["100", "4000002", "500003155", "7"],
            # chunk widths 6-6-4, nested
            {"a": [{"x": "424242"}, "424242"], "b": "4242"},
            # keys only, in order
            {"4000002500": "first", "003155": "second"},
            # a key followed by its value
            {"42424242": "42424242"},
        ],
    )
    def test_split_card_is_withheld(self, value):
        assert redact_value(value) == REDACTED

    def test_the_smallest_container_holding_the_split_is_withheld(self):
        assert redact_value([{"4242424242": 1, "424242": 2}, "ok"]) == [REDACTED, "ok"]

    @pytest.mark.parametrize(
        "value",
        [
            {"id": "12345678", "ref": "87654321", "count": 3},
            ["2019", "2020", "2021", "2022", "2023"],
            {"10001": "NY", "94110": "SF"},
            # numbers are not read as chunks of arbitrary width (stated limit)
            [42424242, 42424242],
        ],
    )
    def test_non_cards_and_number_chunks_stay(self, value):
        assert redact_value(value) == value
