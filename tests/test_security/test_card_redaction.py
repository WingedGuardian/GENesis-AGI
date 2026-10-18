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


class TestArmedMode:
    """While a payment tab is armed (``CardRedactingFilter.armed``), a record
    keeps only its format string: no args, no traceback."""

    @staticmethod
    def _record(exc: bool = True) -> logging.LogRecord:
        try:
            raise RuntimeError("cvc 739")
        except RuntimeError:
            info = __import__("sys").exc_info() if exc else None
        return logging.LogRecord("t", logging.ERROR, __file__, 1, "filled %s", ("739",), info)

    @pytest.mark.parametrize("probe", [lambda: True, lambda: 1 / 0])
    def test_args_and_traceback_are_withheld(self, probe):
        f = CardRedactingFilter()
        f.armed = probe  # a probe that raises counts as armed
        record = self._record()
        assert f.filter(record) is True
        text = logging.Formatter().format(record)
        assert "739" not in text and "RuntimeError" not in text
        assert text == "filled %s [args withheld: payment tab armed]"

    def test_the_format_string_itself_is_still_card_redacted(self):
        f = CardRedactingFilter()
        f.armed = lambda: True
        record = logging.LogRecord("t", logging.INFO, __file__, 1, f"saw {_CARD}", None, None)
        f.filter(record)
        assert _CARD not in record.getMessage() and REDACTED in record.getMessage()

    def test_unarmed_keeps_the_ordinary_redaction(self):
        f = CardRedactingFilter()
        f.armed = lambda: False
        record = self._record()
        f.filter(record)
        assert "filled 739" in record.getMessage() and "RuntimeError" in record.getMessage()
