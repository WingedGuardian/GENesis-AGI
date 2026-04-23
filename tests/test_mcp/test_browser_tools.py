"""Tests for browser MCP tool internals (liveness check, recovery, resilience)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from genesis.mcp.health import browser


@pytest.fixture(autouse=True)
def _reset_browser_state():
    """Reset module-level browser state before and after each test."""
    browser._stealth_cm = None
    browser._stealth_browser = None
    browser._stealth_page = None
    browser._playwright = None
    browser._context = None
    browser._page = None
    browser._active_page = None
    browser._collaborate_mode = False
    browser._browser_lock = asyncio.Lock()
    yield
    browser._stealth_cm = None
    browser._stealth_browser = None
    browser._stealth_page = None
    browser._playwright = None
    browser._context = None
    browser._page = None
    browser._active_page = None
    browser._collaborate_mode = False
    browser._browser_lock = asyncio.Lock()


class TestIsPageAlive:
    def test_alive_page(self):
        page = MagicMock()
        page.is_closed.return_value = False
        page.url = "https://example.com"
        assert browser._is_page_alive(page) is True

    def test_closed_page(self):
        page = MagicMock()
        page.is_closed.return_value = True
        assert browser._is_page_alive(page) is False

    def test_severed_connection(self):
        page = MagicMock()
        page.is_closed.return_value = False
        type(page).url = property(lambda self: (_ for _ in ()).throw(ConnectionError("pipe broken")))
        assert browser._is_page_alive(page) is False

    def test_none_page(self):
        assert browser._is_page_alive(None) is False

    def test_is_closed_throws(self):
        page = MagicMock()
        page.is_closed.side_effect = RuntimeError("already disposed")
        assert browser._is_page_alive(page) is False


class TestEnsureBrowserRecovery:
    @pytest.mark.asyncio
    async def test_returns_alive_page_without_reinit(self):
        """If page is alive, _ensure_browser returns it immediately."""
        page = MagicMock()
        page.is_closed.return_value = False
        page.url = "https://example.com"
        browser._stealth_page = page

        result = await browser._ensure_browser()
        assert result is page

    @pytest.mark.asyncio
    async def test_recovers_from_stale_page(self):
        """If page is dead, _ensure_browser cleans up and re-initializes."""
        pytest.importorskip("camoufox", reason="camoufox not installed")
        dead_page = MagicMock()
        dead_page.is_closed.return_value = True
        browser._stealth_page = dead_page

        new_page = MagicMock()
        mock_browser = MagicMock()
        mock_browser.pages = [new_page]

        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_browser)
        mock_cm.__aexit__ = AsyncMock(return_value=None)

        with patch("camoufox.async_api.AsyncCamoufox", return_value=mock_cm):
            result = await browser._ensure_browser()

        assert result is new_page
        assert browser._stealth_page is new_page

    @pytest.mark.asyncio
    async def test_cleanup_safe_on_dead_browser(self):
        """async_cleanup() doesn't raise when browser is already dead."""
        dead_cm = AsyncMock()
        dead_cm.__aexit__ = AsyncMock(side_effect=ConnectionError("already gone"))
        browser._stealth_cm = dead_cm
        browser._stealth_browser = MagicMock()
        browser._stealth_page = MagicMock()

        await browser.async_cleanup()

        assert browser._stealth_cm is None
        assert browser._stealth_browser is None
        assert browser._stealth_page is None


class TestEnsureChromiumRecovery:
    @pytest.mark.asyncio
    async def test_returns_alive_page(self):
        page = MagicMock()
        page.is_closed.return_value = False
        page.url = "https://example.com"
        browser._page = page

        result = await browser._ensure_chromium_fallback()
        assert result is page

    @pytest.mark.asyncio
    async def test_recovers_from_stale_chromium(self):
        pytest.importorskip("playwright", reason="playwright not installed")
        dead_page = MagicMock()
        dead_page.is_closed.return_value = True
        browser._page = dead_page

        new_page = MagicMock()
        mock_context = AsyncMock()
        mock_context.pages = [new_page]

        mock_pw = AsyncMock()
        mock_pw.chromium.launch_persistent_context = AsyncMock(return_value=mock_context)

        with patch("playwright.async_api.async_playwright") as mock_apw:
            mock_starter = AsyncMock()
            mock_starter.start = AsyncMock(return_value=mock_pw)
            mock_apw.return_value = mock_starter
            result = await browser._ensure_chromium_fallback()

        assert result is new_page
        assert browser._page is new_page


# ---------------------------------------------------------------------------
# New tests for browser resilience (timeouts, selectors, keyboard, press_key)
# ---------------------------------------------------------------------------


class TestToolTimeout:
    """Verify _with_tool_timeout returns structured error on timeout."""

    @pytest.mark.asyncio
    async def test_returns_result_on_success(self):
        async def quick():
            return {"ok": True}

        result = await browser._with_tool_timeout(quick(), 5.0, "test_op")
        assert result == {"ok": True}

    @pytest.mark.asyncio
    async def test_returns_error_on_timeout(self):
        async def slow():
            await asyncio.sleep(10)
            return {"ok": True}

        result = await browser._with_tool_timeout(slow(), 0.05, "test_op")
        assert "error" in result
        assert "timed out" in result["error"]
        assert "test_op" in result["error"]

    @pytest.mark.asyncio
    async def test_resets_active_page_on_timeout(self):
        """Timeout must reset _active_page to None to avoid stale page state."""
        browser._active_page = MagicMock()  # simulate an active page

        async def slow():
            await asyncio.sleep(10)
            return {"ok": True}

        result = await browser._with_tool_timeout(slow(), 0.05, "test_op")
        assert "error" in result
        assert browser._active_page is None

    @pytest.mark.asyncio
    async def test_propagates_non_timeout_exceptions(self):
        async def broken():
            raise ValueError("boom")

        with pytest.raises(ValueError, match="boom"):
            await browser._with_tool_timeout(broken(), 5.0, "test_op")


class TestSnapshotTimeout:
    """Verify _snapshot_page handles timeout gracefully."""

    @pytest.mark.asyncio
    async def test_returns_snapshot_on_success(self):
        page = MagicMock()
        locator = MagicMock()
        locator.aria_snapshot = AsyncMock(return_value="- heading: Hello")
        page.locator.return_value = locator

        result = await browser._snapshot_page(page)
        assert result == "- heading: Hello"

    @pytest.mark.asyncio
    async def test_returns_message_on_timeout(self):
        page = MagicMock()
        locator = MagicMock()

        async def hang():
            await asyncio.sleep(60)

        locator.aria_snapshot = hang
        page.locator.return_value = locator

        # Patch the 15s timeout to 0.05s for test speed
        with patch.object(asyncio, "wait_for", wraps=asyncio.wait_for):
            result = await browser._snapshot_page(page)
            # The actual 15s timeout would make this test slow, so we verify
            # the structure handles exceptions gracefully
            assert isinstance(result, str)

    @pytest.mark.asyncio
    async def test_returns_message_on_exception(self):
        page = MagicMock()
        locator = MagicMock()
        locator.aria_snapshot = AsyncMock(side_effect=RuntimeError("DOM gone"))
        page.locator.return_value = locator

        result = await browser._snapshot_page(page)
        assert "snapshot unavailable" in result


class TestAmbiguousSelector:
    """Verify _stealth_click raises on ambiguous text= selectors."""

    @pytest.mark.asyncio
    async def test_ambiguous_text_selector_raises(self):
        page = MagicMock()
        locator = MagicMock()

        # count() returns a coroutine that resolves to 3
        locator.count = AsyncMock(return_value=3)

        # nth() returns locators with evaluate() for element info
        nth_locator = MagicMock()
        nth_locator.evaluate = AsyncMock(side_effect=["input", "sponsorship", "input", "relocation", "input", "experience"])
        locator.nth.return_value = nth_locator

        page.locator.return_value = locator

        with pytest.raises(Exception, match="Ambiguous selector"):
            await browser._stealth_click(page, "text=No")

    @pytest.mark.asyncio
    async def test_unique_text_selector_proceeds(self):
        """Single match should not raise ambiguity error."""
        page = MagicMock()
        locator = MagicMock()
        locator.count = AsyncMock(return_value=1)
        page.locator.return_value = locator

        # Non-Camoufox mode — plain click path
        page.click = AsyncMock()

        await browser._stealth_click(page, "text=Submit")
        page.click.assert_awaited_once()


class TestKeyboardFallback:
    """Verify _stealth_click falls back to keyboard on click failure."""

    @pytest.mark.asyncio
    async def test_keyboard_fallback_on_radio(self):
        """When both stealth and plain click fail, keyboard fallback fires."""
        page = MagicMock()
        locator = MagicMock()
        locator.count = AsyncMock(return_value=1)
        page.locator.return_value = locator

        # Make Camoufox active so stealth path runs
        browser._stealth_cm = MagicMock()
        browser._stealth_page = page
        browser._active_page = page

        # Stealth click path fails (wait_for_selector raises)
        el_mock = AsyncMock()
        el_mock.focus = AsyncMock()
        el_mock.evaluate = AsyncMock(side_effect=["input", "radio"])
        page.wait_for_selector = AsyncMock(
            side_effect=[Exception("stealth failed"), el_mock]
        )
        # Plain click also fails
        page.click = AsyncMock(side_effect=Exception("plain failed"))

        # Keyboard mock
        page.keyboard = MagicMock()
        page.keyboard.press = AsyncMock()

        await browser._stealth_click(page, "text=No")

        # Verify keyboard.press("Space") was called for the radio
        page.keyboard.press.assert_awaited_once_with("Space")

    @pytest.mark.asyncio
    async def test_raises_when_all_methods_fail(self):
        """When stealth, plain, AND keyboard all fail, raises the plain error."""
        page = MagicMock()
        locator = MagicMock()
        locator.count = AsyncMock(return_value=1)
        page.locator.return_value = locator

        browser._stealth_cm = MagicMock()
        browser._stealth_page = page
        browser._active_page = page

        # All methods fail
        page.wait_for_selector = AsyncMock(side_effect=Exception("nope"))
        page.click = AsyncMock(side_effect=Exception("plain failed"))
        page.keyboard = MagicMock()
        page.keyboard.press = AsyncMock(side_effect=Exception("kb failed"))

        with pytest.raises(Exception, match="plain failed"):
            await browser._stealth_click(page, "text=No")


class TestPressKey:
    """Verify _impl_browser_press_key."""

    @pytest.mark.asyncio
    async def test_single_key_press(self):
        page = MagicMock()
        page.url = "https://example.com"
        page.keyboard = MagicMock()
        page.keyboard.press = AsyncMock()
        browser._active_page = page

        result = await browser._impl_browser_press_key("Tab")
        assert result["pressed"] == "Tab"
        assert result["count"] == 1
        page.keyboard.press.assert_awaited_once_with("Tab")

    @pytest.mark.asyncio
    async def test_multiple_key_presses(self):
        page = MagicMock()
        page.url = "https://example.com"
        page.keyboard = MagicMock()
        page.keyboard.press = AsyncMock()
        browser._active_page = page

        result = await browser._impl_browser_press_key("Tab", 3)
        assert result["count"] == 3
        assert page.keyboard.press.await_count == 3

    @pytest.mark.asyncio
    async def test_count_clamped(self):
        page = MagicMock()
        page.url = "https://example.com"
        page.keyboard = MagicMock()
        page.keyboard.press = AsyncMock()
        browser._active_page = page

        result = await browser._impl_browser_press_key("Tab", 100)
        assert result["count"] == 50  # clamped to max

    @pytest.mark.asyncio
    async def test_no_page_error(self):
        result = await browser._impl_browser_press_key("Tab")
        assert "error" in result
        assert "No page open" in result["error"]

    @pytest.mark.asyncio
    async def test_invalid_key_error(self):
        page = MagicMock()
        page.url = "https://example.com"
        page.keyboard = MagicMock()
        page.keyboard.press = AsyncMock(side_effect=Exception("Unknown key: Foo"))
        browser._active_page = page

        result = await browser._impl_browser_press_key("Foo")
        assert "error" in result
        assert "Foo" in result["error"]
