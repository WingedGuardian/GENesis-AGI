"""Tests for the browser card guard: card-field detection on a live page."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from genesis.mcp.health import browser_card_guard as cg

CARD_ATTRS = {"tag": "input", "autocomplete": "cc-number", "name": "cardnumber"}
PLAIN_ATTRS = {"tag": "input", "autocomplete": "email", "name": "email"}


def _element(tag: str, attrs: dict | None = None, child_frame=None, opaque: bool = False):
    """A fake element handle: first evaluate() returns the tag info, the next the attrs."""
    el = MagicMock()
    el.evaluate = AsyncMock(side_effect=[{"tag": tag, "opaque": opaque}, attrs or {}])
    el.content_frame = AsyncMock(return_value=child_frame)
    return el


def _frame(element):
    handle = MagicMock()
    handle.as_element = MagicMock(return_value=element)
    handle.dispose = AsyncMock()
    frame = MagicMock()
    frame.evaluate_handle = AsyncMock(return_value=handle)
    return frame


def _page(main_frame):
    page = MagicMock()
    page.main_frame = main_frame
    return page


class TestFocusedCardField:
    async def test_card_field_in_main_frame(self):
        page = _page(_frame(_element("input", CARD_ATTRS)))
        assert await cg.focused_card_field(page) is True

    async def test_plain_field_in_main_frame(self):
        page = _page(_frame(_element("input", PLAIN_ATTRS)))
        assert await cg.focused_card_field(page) is False

    async def test_nothing_focused(self):
        handle = MagicMock()
        handle.as_element = MagicMock(return_value=None)
        handle.dispose = AsyncMock()
        frame = MagicMock()
        frame.evaluate_handle = AsyncMock(return_value=handle)
        assert await cg.focused_card_field(_page(frame)) is False

    async def test_card_field_inside_iframe(self):
        inner = _frame(_element("input", CARD_ATTRS))
        outer = _frame(_element("iframe", child_frame=inner))
        assert await cg.focused_card_field(_page(outer)) is True

    async def test_card_field_two_iframes_deep(self):
        innermost = _frame(_element("input", CARD_ATTRS))
        middle = _frame(_element("iframe", child_frame=innermost))
        outer = _frame(_element("iframe", child_frame=middle))
        assert await cg.focused_card_field(_page(outer)) is True

    async def test_plain_field_inside_iframe(self):
        inner = _frame(_element("input", PLAIN_ATTRS))
        outer = _frame(_element("iframe", child_frame=inner))
        assert await cg.focused_card_field(_page(outer)) is False

    async def test_opaque_custom_element_is_unverified(self):
        # Focus on a custom element whose shadow root is closed (or absent):
        # its inside cannot be read, so it may hold a card field.
        page = _page(_frame(_element("pay-field", {"tag": "pay-field"}, opaque=True)))
        assert await cg.focused_card_field(page) is None

    async def test_opaque_custom_element_with_card_attributes_is_card(self):
        page = _page(_frame(_element("pay-field", CARD_ATTRS, opaque=True)))
        assert await cg.focused_card_field(page) is True

    async def test_iframe_without_reachable_frame_is_unverified(self):
        outer = _frame(_element("iframe", child_frame=None))
        assert await cg.focused_card_field(_page(outer)) is None

    async def test_evaluation_error_is_unverified(self):
        frame = MagicMock()
        frame.evaluate_handle = AsyncMock(side_effect=RuntimeError("frame detached"))
        assert await cg.focused_card_field(_page(frame)) is None

    async def test_hung_evaluation_is_unverified(self, monkeypatch):
        monkeypatch.setattr(cg, "_STEP_TIMEOUT_S", 0.05)

        async def hang(*_a, **_k):
            await asyncio.sleep(10)

        frame = MagicMock()
        frame.evaluate_handle = hang
        assert await cg.focused_card_field(_page(frame)) is None

    async def test_hung_handle_disposal_is_bounded(self, monkeypatch):
        """Disposal is cleanup, but a wedged transport must not hang the probe:
        the answer already read (a card field) still comes back."""
        monkeypatch.setattr(cg, "_STEP_TIMEOUT_S", 0.05)

        async def hang(*_a, **_k):
            await asyncio.sleep(10)

        frame = _frame(_element("input", CARD_ATTRS))
        frame.evaluate_handle.return_value.dispose = hang
        result = await asyncio.wait_for(cg.focused_card_field(_page(frame)), timeout=2)
        assert result is True

    async def test_runaway_nesting_is_unverified(self):
        # A frame whose focused element is always another iframe.
        frame = MagicMock()

        async def evaluate_handle(_expr):
            handle = MagicMock()
            handle.as_element = MagicMock(return_value=_element("iframe", child_frame=frame))
            handle.dispose = AsyncMock()
            return handle

        frame.evaluate_handle = evaluate_handle
        assert await cg.focused_card_field(_page(frame)) is None


class TestTargetIsCardField:
    async def test_card_target(self):
        locator = MagicMock()
        locator.first.evaluate = AsyncMock(return_value=CARD_ATTRS)
        page = MagicMock()
        page.locator = MagicMock(return_value=locator)
        assert await cg.target_is_card_field(page, "#cc") is True

    async def test_plain_target(self):
        locator = MagicMock()
        locator.first.evaluate = AsyncMock(return_value=PLAIN_ATTRS)
        page = MagicMock()
        page.locator = MagicMock(return_value=locator)
        assert await cg.target_is_card_field(page, "#email") is False

    async def test_unresolved_target_is_none(self):
        locator = MagicMock()
        locator.first.evaluate = AsyncMock(side_effect=TimeoutError("no element"))
        page = MagicMock()
        page.locator = MagicMock(return_value=locator)
        assert await cg.target_is_card_field(page, "#missing") is None


class TestRefuseIfFocusedCardField:
    async def test_raises_on_card_field(self, monkeypatch):
        monkeypatch.setattr(cg, "focused_card_field", AsyncMock(return_value=True))
        with pytest.raises(cg.CardFieldRefused) as exc:
            await cg.refuse_if_focused_card_field(MagicMock())
        assert str(exc.value) == cg.REFUSE_FIELD

    async def test_raises_when_unverified(self, monkeypatch):
        monkeypatch.setattr(cg, "focused_card_field", AsyncMock(return_value=None))
        with pytest.raises(cg.CardFieldRefused) as exc:
            await cg.refuse_if_focused_card_field(MagicMock())
        assert str(exc.value) == cg.REFUSE_UNVERIFIED

    async def test_passes_on_plain_field(self, monkeypatch):
        monkeypatch.setattr(cg, "focused_card_field", AsyncMock(return_value=False))
        await cg.refuse_if_focused_card_field(MagicMock())


class TestKeyAllowlist:
    @pytest.mark.parametrize(
        "key", ["Tab", "Shift+Tab", "Enter", "Escape", "ArrowDown", "Backspace", "Home", "Space"]
    )
    def test_navigation_keys_allowed(self, key):
        assert cg.key_allowed_in_card_field(key)

    @pytest.mark.parametrize(
        "key", ["4", "a", "Digit4", "Numpad4", "Shift+4", "Control+v", "Meta+v", "tab"]
    )
    def test_typing_keys_not_allowed(self, key):
        assert not cg.key_allowed_in_card_field(key)


def test_refusal_messages_carry_no_placeholder_for_values():
    # The refusals are fixed text: nothing the caller passed is echoed back.
    for message in (
        cg.REFUSE_VALUE,
        cg.REFUSE_FIELD,
        cg.REFUSE_UNVERIFIED,
        cg.REFUSE_TARGET_UNVERIFIED,
        cg.REFUSE_RUN_JS,
    ):
        assert "{" not in message and "%" not in message
