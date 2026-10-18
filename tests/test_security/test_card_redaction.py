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

    def test_text_without_a_card_is_unchanged(self):
        for text in ("order 12345", '{"a": 1}', "[1, 2", "- heading: x"):
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
