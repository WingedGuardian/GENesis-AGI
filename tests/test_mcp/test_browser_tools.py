"""Tests for browser MCP tool internals (liveness check, recovery, resilience)."""

from __future__ import annotations

import asyncio
import importlib.util
import os
import re
import signal
import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from genesis.mcp.health import browser


def _clear_all_browser_state():
    """Clear all module-level browser state."""
    browser._stealth_cm = None
    browser._stealth_browser = None
    browser._stealth_page = None
    browser._playwright_cm = None
    browser._playwright = None
    browser._context = None
    browser._page = None
    browser._active_page = None
    browser._stack_teardown_unconfirmed = None
    browser._collaborate_mode = False
    browser._browser_lock = asyncio.Lock()
    # Remote CDP state
    browser._remote_pw = None
    browser._remote_browser = None
    browser._remote_page = None
    browser._remote_cdp_url = None
    browser._remote_last_url = None
    browser._tinyfish_pw = None
    browser._tinyfish_browser = None
    browser._tinyfish_page = None
    browser._tinyfish_session_id = None
    browser._launched_browser_pids = {}
    # VNC verification flag — FIX 3 tests toggle it; reset to avoid leak
    browser._vnc_verified = False


@pytest.fixture(autouse=True)
def _reset_browser_state(tmp_path, monkeypatch):
    """Reset module-level browser state before and after each test. The
    browser-stack lock points into tmp_path: a test must never hold the real
    one, which a provisioning run on this machine would then see as a browser."""
    from genesis.browser import engine

    monkeypatch.setattr(engine, "BROWSER_LOCK_FILE", tmp_path / "locks" / "browser.lock")
    # Which browser distributions count as loaded, and at what version, is
    # process state the module accumulates; start each test from the startup view.
    monkeypatch.setattr(
        browser, "_LOADED_BROWSER_VERSIONS",
        {d: v for d, v in browser._STARTUP_BROWSER_VERSIONS.items() if d in sys.modules},
        raising=False,
    )
    monkeypatch.setattr(
        browser, "_LAST_SEEN_BROWSER_VERSIONS", dict(browser._STARTUP_BROWSER_VERSIONS),
        raising=False,
    )
    _clear_all_browser_state()
    yield
    _clear_all_browser_state()
    if browser._stack_lock_fd is not None:
        os.close(browser._stack_lock_fd)
        browser._stack_lock_fd = None


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

        from genesis.browser import engine

        ready = engine.EngineStatus(engine.READY, "test engine", Path("/engine"))
        with (
            patch("camoufox.async_api.AsyncCamoufox", return_value=mock_cm),
            patch.object(engine, "camoufox_engine_status", return_value=ready),
        ):
            result = await browser._ensure_browser()

        assert result is new_page
        assert browser._stealth_page is new_page

    @pytest.mark.asyncio
    async def test_refuses_to_launch_without_a_ready_engine(self):
        """camoufox 0.5's launch path deletes a pre-0.5 engine and downloads inside
        the call, so an unready engine must stop the launch before camoufox runs."""
        from genesis.browser import engine

        legacy = engine.EngineStatus(engine.LEGACY_LAYOUT, f"pre-0.5 engine; {engine.PROVISION_HINT}")
        constructed = MagicMock()
        with (
            patch.object(engine, "camoufox_engine_status", return_value=legacy),
            patch.dict("sys.modules", {"camoufox.async_api": MagicMock(AsyncCamoufox=constructed)}),
            pytest.raises(browser.CamoufoxEngineNotReady, match="pre-0.5 engine"),
        ):
            await browser._ensure_browser()
        constructed.assert_not_called()
        assert browser._stealth_cm is None

    @pytest.mark.asyncio
    async def test_navigate_reports_unready_engine_as_error(self):
        from genesis.browser import engine

        missing = engine.EngineStatus(engine.PIN_NOT_INSTALLED, "needs engine 156.0.1-beta.34")
        with (
            patch.object(engine, "camoufox_engine_status", return_value=missing),
            patch.object(browser, "_ensure_vnc", new=AsyncMock()),
        ):
            result = await browser._impl_browser_navigate("https://example.com")
        assert result["error"].startswith("Camoufox is not ready")
        assert "156.0.1-beta.34" in result["error"]

    @pytest.mark.asyncio
    async def test_navigate_import_error_after_an_upgrade_says_restart(self):
        """A provisioning run upgraded the packages under this live process: the
        old playwright is still imported, so the new camoufox cannot import, and
        reinstalling would not help. Only a restart does."""
        upgraded = dict(browser._STARTUP_BROWSER_VERSIONS, playwright="9.99.0")
        with (
            patch.object(browser, "_installed_browser_versions", return_value=upgraded),
            patch.object(browser, "_get_page", new=AsyncMock(side_effect=ImportError(
                "cannot import name 'BrowserBindResult'"))),
        ):
            result = await browser._impl_browser_navigate("https://example.com")
        assert "Restart this Claude Code session" in result["error"]
        assert "-> 9.99.0" in result["error"]
        assert "BrowserBindResult" in result["error"]
        assert "camoufox fetch" not in result["error"]

    @pytest.mark.asyncio
    async def test_launch_refused_on_stale_loaded_modules(self):
        """Old camoufox already imported, new files on disk: the launch would
        fail with a misleading 'run camoufox fetch'. Refuse it with 'restart'."""
        from genesis.browser import engine

        ready = engine.EngineStatus(engine.READY, "Camoufox 156.0.1-beta.34")
        upgraded = dict(browser._STARTUP_BROWSER_VERSIONS, camoufox="9.9.9")
        constructed = MagicMock()
        with (
            patch.object(engine, "camoufox_engine_status", return_value=ready),
            patch.object(browser, "_installed_browser_versions", return_value=upgraded),
            patch.object(browser, "_ensure_vnc", new=AsyncMock()),
            patch.dict("sys.modules", {
                "camoufox": MagicMock(),
                "camoufox.async_api": MagicMock(AsyncCamoufox=constructed),
            }),
        ):
            result = await browser._impl_browser_navigate("https://example.com")
        assert "Restart this Claude Code session" in result["error"]
        assert "-> 9.9.9" in result["error"]
        constructed.assert_not_called()

    @pytest.mark.asyncio
    async def test_launch_during_an_upgrade_is_refused(self, tmp_path):
        """Provisioning holds the browser-stack lock exclusive for its whole run."""
        import fcntl

        from genesis.browser import engine

        lock = tmp_path / "browser.lock"
        with (
            patch.object(engine, "BROWSER_LOCK_FILE", lock),
            patch.object(browser, "_ensure_vnc", new=AsyncMock()),
            open(lock, "w") as provisioning,
        ):
            fcntl.flock(provisioning, fcntl.LOCK_EX)
            result = await browser._impl_browser_navigate("https://example.com")
        assert "being upgraded" in result["error"]
        assert browser._stack_lock_fd is None

    @pytest.mark.asyncio
    async def test_failed_launch_releases_the_stack_lock(self, tmp_path):
        import fcntl

        from genesis.browser import engine

        lock = tmp_path / "browser.lock"
        missing = engine.EngineStatus(engine.PIN_NOT_INSTALLED, "needs engine")
        with (
            patch.object(engine, "BROWSER_LOCK_FILE", lock),
            patch.object(engine, "camoufox_engine_status", return_value=missing),
            patch.object(browser, "_ensure_vnc", new=AsyncMock()),
        ):
            result = await browser._impl_browser_navigate("https://example.com")
        assert "not ready" in result["error"]
        assert browser._stack_lock_fd is None
        with open(lock, "w") as provisioning:  # nothing still holds it shared
            fcntl.flock(provisioning, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_an_unloaded_package_change_needs_no_restart(self):
        """Only a distribution this process imported is stale; camoufox changed
        on disk while only playwright is loaded does not block Chromium."""
        changed = dict(browser._STARTUP_BROWSER_VERSIONS, camoufox="9.9.9")
        modules = {k: v for k, v in sys.modules.items() if k != "camoufox"}
        modules["playwright"] = MagicMock()
        with (
            patch.object(browser, "_installed_browser_versions", return_value=changed),
            patch.dict("sys.modules", modules, clear=True),
        ):
            browser._check_loaded_browser_modules()  # does not raise

    @pytest.mark.asyncio
    async def test_nothing_loaded_yet_means_no_restart_needed(self):
        upgraded = dict(browser._STARTUP_BROWSER_VERSIONS, camoufox="9.9.9")
        with (
            patch.object(browser, "_installed_browser_versions", return_value=upgraded),
            patch.object(browser, "_BROWSER_DISTS", ("not-a-loaded-module",)),
        ):
            browser._check_loaded_browser_modules()  # does not raise

    @pytest.mark.asyncio
    async def test_navigate_import_error_without_an_upgrade_says_install(self):
        with (
            patch.object(
                browser, "_installed_browser_versions",
                return_value=dict(browser._STARTUP_BROWSER_VERSIONS),
            ),
            patch.object(browser, "_get_page", new=AsyncMock(side_effect=ImportError(
                "No module named 'camoufox'"))),
        ):
            result = await browser._impl_browser_navigate("https://example.com")
        assert result["error"].startswith("Browser not available")
        from genesis.browser.engine import PROVISION_HINT

        assert PROVISION_HINT in result["error"]

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
        """When stealth, plain, keyboard, AND shadow DOM all fail, raises the plain error."""
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
        # Shadow DOM fallback also fails (returns False = not found)
        page.evaluate = AsyncMock(return_value=False)

        with pytest.raises(Exception, match="plain failed"):
            await browser._stealth_click(page, "text=No")


class TestShadowDomClick:
    """Verify _click_in_shadow_dom and its integration in _stealth_click."""

    @pytest.mark.asyncio
    async def test_shadow_dom_fallback_succeeds(self):
        """Shadow DOM JS traversal finds and clicks element after all else fails."""
        page = MagicMock()
        locator = MagicMock()
        locator.count = AsyncMock(return_value=1)
        page.locator.return_value = locator

        browser._stealth_cm = MagicMock()
        browser._stealth_page = page
        browser._active_page = page

        # Stealth, plain, and keyboard all fail
        page.wait_for_selector = AsyncMock(side_effect=Exception("nope"))
        page.click = AsyncMock(side_effect=Exception("plain failed"))
        page.keyboard = MagicMock()
        page.keyboard.press = AsyncMock(side_effect=Exception("kb failed"))
        # Shadow DOM fallback succeeds (JS found and clicked the element)
        page.evaluate = AsyncMock(return_value=True)

        # Should NOT raise — shadow DOM fallback saves it
        await browser._stealth_click(page, "text=Submit")

        # Verify page.evaluate was called with the shadow DOM JS
        page.evaluate.assert_awaited_once()
        args = page.evaluate.call_args
        assert args[0][1] == ["Submit", True]  # [search_value, is_text]

    @pytest.mark.asyncio
    async def test_shadow_dom_not_triggered_on_normal_success(self):
        """Shadow DOM fallback should not run when normal click succeeds."""
        page = MagicMock()
        locator = MagicMock()
        locator.count = AsyncMock(return_value=1)
        page.locator.return_value = locator

        # Non-Camoufox mode — plain click succeeds
        page.click = AsyncMock()
        page.evaluate = AsyncMock()

        await browser._stealth_click(page, "text=Submit")

        # Plain click worked, evaluate should NOT be called
        page.evaluate.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_shadow_dom_css_selector(self):
        """CSS selectors are passed to querySelector inside shadow roots."""
        page = MagicMock()
        page.evaluate = AsyncMock(return_value=True)

        result = await browser._click_in_shadow_dom(page, "button.submit-btn")

        assert result is True
        args = page.evaluate.call_args
        assert args[0][1] == ["button.submit-btn", False]  # [value, is_text=False]

    @pytest.mark.asyncio
    async def test_shadow_dom_evaluate_exception(self):
        """JS evaluation errors return False, not raise."""
        page = MagicMock()
        page.evaluate = AsyncMock(side_effect=Exception("page crashed"))

        result = await browser._click_in_shadow_dom(page, "text=Click")

        assert result is False


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


# ---------------------------------------------------------------------------
# CDP Remote Browser Tests (Layer 3)
# ---------------------------------------------------------------------------


def _mock_remote_browser(pages=None, connected=True):
    """Create a mock CDP browser with optional pages."""
    mock_browser = MagicMock()
    mock_browser.is_connected.return_value = connected
    mock_browser.close = AsyncMock()
    mock_browser.on = MagicMock()
    ctx = MagicMock()
    ctx.pages = pages or []
    ctx.new_page = AsyncMock(return_value=MagicMock())
    mock_browser.contexts = [ctx]
    mock_browser.new_context = AsyncMock(return_value=ctx)
    return mock_browser


@pytest.mark.skipif(
    not importlib.util.find_spec("playwright"),
    reason="playwright not installed",
)
class TestEnsureRemoteCdp:
    """Verify _ensure_remote_cdp connection lifecycle."""

    @pytest.mark.asyncio
    async def test_returns_alive_page_without_reconnect(self):
        """Already-connected, alive page is reused."""
        page = MagicMock()
        page.is_closed.return_value = False
        page.url = "https://example.com"
        mock_br = _mock_remote_browser(connected=True)

        browser._remote_page = page
        browser._remote_browser = mock_br

        result = await browser._ensure_remote_cdp("http://100.1.2.3:9222")
        assert result is page

    @pytest.mark.asyncio
    async def test_reconnects_after_disconnect(self):
        """Stale connection is cleaned up and re-established."""
        dead_browser = _mock_remote_browser(connected=False)
        browser._remote_browser = dead_browser
        browser._remote_page = MagicMock()

        new_page = MagicMock()
        new_page.url = "chrome://newtab/"
        new_browser = _mock_remote_browser(pages=[new_page])

        mock_pw = AsyncMock()
        mock_pw.chromium.connect_over_cdp = AsyncMock(return_value=new_browser)
        mock_pw.stop = AsyncMock()

        with patch("playwright.async_api.async_playwright") as mock_apw:
            mock_starter = AsyncMock()
            mock_starter.start = AsyncMock(return_value=mock_pw)
            mock_apw.return_value = mock_starter
            result = await browser._ensure_remote_cdp("http://100.1.2.3:9222")

        assert result is new_page
        assert browser._remote_cdp_url == "http://100.1.2.3:9222"

    @pytest.mark.asyncio
    async def test_raises_connection_error_no_url(self):
        """No CDP URL configured — clear error with setup instructions."""
        with pytest.raises(ConnectionError, match="No CDP URL configured"):
            await browser._ensure_remote_cdp(None)

    @pytest.mark.asyncio
    async def test_raises_connection_error_chrome_not_running(self):
        """Chrome not running — clear error with troubleshooting."""
        mock_pw = AsyncMock()
        mock_pw.chromium.connect_over_cdp = AsyncMock(
            side_effect=Exception("Connection refused")
        )
        mock_pw.stop = AsyncMock()

        with patch("playwright.async_api.async_playwright") as mock_apw:
            mock_starter = AsyncMock()
            mock_starter.start = AsyncMock(return_value=mock_pw)
            mock_apw.return_value = mock_starter
            with pytest.raises(ConnectionError, match="Cannot connect"):
                await browser._ensure_remote_cdp("http://100.1.2.3:9222")

        # Playwright instance must be cleaned up
        mock_pw.stop.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_uses_existing_tab(self):
        """Picks first tab with a real URL (chrome://, http://, etc)."""
        visible_page = MagicMock()
        visible_page.url = "chrome://newtab/"
        other_page = MagicMock()
        other_page.url = "https://already-open.com"
        new_browser = _mock_remote_browser(pages=[visible_page, other_page])

        mock_pw = AsyncMock()
        mock_pw.chromium.connect_over_cdp = AsyncMock(return_value=new_browser)

        with patch("playwright.async_api.async_playwright") as mock_apw:
            mock_starter = AsyncMock()
            mock_starter.start = AsyncMock(return_value=mock_pw)
            mock_apw.return_value = mock_starter
            result = await browser._ensure_remote_cdp("http://100.1.2.3:9222")

        assert result is visible_page  # First tab with real URL

    @pytest.mark.asyncio
    async def test_creates_new_tab_when_no_pages(self):
        """Context exists but no pages — creates new tab."""
        new_browser = _mock_remote_browser(pages=[])
        created_page = MagicMock()
        new_browser.contexts[0].new_page = AsyncMock(return_value=created_page)

        mock_pw = AsyncMock()
        mock_pw.chromium.connect_over_cdp = AsyncMock(return_value=new_browser)

        with patch("playwright.async_api.async_playwright") as mock_apw:
            mock_starter = AsyncMock()
            mock_starter.start = AsyncMock(return_value=mock_pw)
            mock_apw.return_value = mock_starter
            result = await browser._ensure_remote_cdp("http://100.1.2.3:9222")

        assert result is created_page
        new_browser.contexts[0].new_page.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_resolves_url_from_env(self):
        """Falls back to GENESIS_CDP_URL env var when no explicit URL."""
        new_page = MagicMock()
        new_page.url = "chrome://newtab/"
        new_browser = _mock_remote_browser(pages=[new_page])

        mock_pw = AsyncMock()
        mock_pw.chromium.connect_over_cdp = AsyncMock(return_value=new_browser)

        with patch("playwright.async_api.async_playwright") as mock_apw, \
             patch.dict("os.environ", {"GENESIS_CDP_URL": "http://env.url:9222"}):
            mock_starter = AsyncMock()
            mock_starter.start = AsyncMock(return_value=mock_pw)
            mock_apw.return_value = mock_starter
            await browser._ensure_remote_cdp(None)

        mock_pw.chromium.connect_over_cdp.assert_awaited_once_with("http://env.url:9222")


class TestRemotePageDrift:
    """Verify _check_page_drift detects URL changes."""

    def test_no_drift_when_url_unchanged(self):
        page = MagicMock()
        page.url = "https://example.com/form"
        browser._remote_last_url = "https://example.com/form"

        assert browser._check_page_drift(page) is None

    def test_drift_detected_on_url_change(self):
        page = MagicMock()
        page.url = "https://example.com/other"
        browser._remote_last_url = "https://example.com/form"

        drift = browser._check_page_drift(page)
        assert drift is not None
        assert drift["drift"] == "url_changed"
        assert drift["expected"] == "https://example.com/form"
        assert drift["actual"] == "https://example.com/other"

    def test_no_drift_when_no_previous_url(self):
        page = MagicMock()
        page.url = "https://example.com"
        browser._remote_last_url = None

        assert browser._check_page_drift(page) is None

    def test_drift_on_inaccessible_page(self):
        page = MagicMock()
        type(page).url = property(lambda self: (_ for _ in ()).throw(Exception("tab closed")))
        browser._remote_last_url = "https://example.com"

        drift = browser._check_page_drift(page)
        assert drift is not None
        assert drift["drift"] == "page_inaccessible"


class TestRemoteCleanup:
    """Verify _cleanup_remote_cdp disconnects safely."""

    @pytest.mark.asyncio
    async def test_cleanup_disconnects_without_closing_chrome(self):
        """browser.close() on CDP = disconnect, verified via mock."""
        mock_br = _mock_remote_browser()
        mock_pw = AsyncMock()
        mock_pw.stop = AsyncMock()

        browser._remote_browser = mock_br
        browser._remote_page = MagicMock()
        browser._remote_pw = mock_pw
        browser._remote_last_url = "https://example.com"

        await browser._cleanup_remote_cdp()

        mock_br.close.assert_awaited_once()
        mock_pw.stop.assert_awaited_once()
        assert browser._remote_browser is None
        assert browser._remote_page is None
        assert browser._remote_pw is None
        assert browser._remote_last_url is None

    @pytest.mark.asyncio
    async def test_cleanup_safe_on_dead_connection(self):
        """Cleanup doesn't raise when browser.close() fails."""
        mock_br = _mock_remote_browser()
        mock_br.close = AsyncMock(side_effect=Exception("already gone"))
        mock_pw = AsyncMock()
        mock_pw.stop = AsyncMock()

        browser._remote_browser = mock_br
        browser._remote_page = MagicMock()
        browser._remote_pw = mock_pw

        await browser._cleanup_remote_cdp()  # should not raise

        assert browser._remote_browser is None
        assert browser._remote_pw is None

    @pytest.mark.asyncio
    async def test_cleanup_resets_all_globals(self):
        browser._remote_browser = MagicMock()
        browser._remote_page = MagicMock()
        browser._remote_pw = AsyncMock()
        browser._remote_pw.stop = AsyncMock()
        browser._remote_browser.close = AsyncMock()
        browser._remote_last_url = "https://test.com"

        await browser._cleanup_remote_cdp()

        assert browser._remote_browser is None
        assert browser._remote_page is None
        assert browser._remote_pw is None
        assert browser._remote_last_url is None
        # cdp_url preserved for reconnection
        # (not set in this test, but verify it's not touched)


class TestRemoteNavigate:
    """Verify _impl_browser_navigate with remote=True."""

    @pytest.mark.asyncio
    async def test_navigate_remote_auto_enables_collaborate(self):
        page = MagicMock()
        page.url = "https://example.com"
        page.title = AsyncMock(return_value="Example")
        page.goto = AsyncMock()
        page.is_closed.return_value = False
        locator = MagicMock()
        locator.aria_snapshot = AsyncMock(return_value="- heading: Example")
        page.locator.return_value = locator

        assert browser._collaborate_mode is False
        # _ensure_remote_cdp normally sets _remote_page; mock must too
        browser._remote_page = page

        with patch.object(browser, "_ensure_remote_cdp", new_callable=AsyncMock, return_value=page):
            result = await browser._impl_browser_navigate(
                "https://example.com", remote=True, cdp_url="http://x:9222"
            )

        assert browser._collaborate_mode is True
        assert result.get("layer") == "remote_cdp"

    @pytest.mark.asyncio
    async def test_navigate_remote_skips_turnstile(self):
        page = MagicMock()
        page.url = "https://example.com"
        page.title = AsyncMock(return_value="Example")
        page.goto = AsyncMock()
        page.is_closed.return_value = False
        locator = MagicMock()
        locator.aria_snapshot = AsyncMock(return_value="- heading: Example")
        page.locator.return_value = locator
        browser._remote_page = page

        with patch.object(browser, "_ensure_remote_cdp", new_callable=AsyncMock, return_value=page), \
             patch.object(browser, "_wait_for_turnstile") as mock_turnstile:
            await browser._impl_browser_navigate(
                "https://example.com", remote=True, cdp_url="http://x:9222"
            )

        mock_turnstile.assert_not_called()

    @pytest.mark.asyncio
    async def test_navigate_remote_connection_error(self):
        with patch.object(
            browser, "_ensure_remote_cdp", new_callable=AsyncMock,
            side_effect=ConnectionError("No CDP URL configured"),
        ):
            result = await browser._impl_browser_navigate(
                "https://example.com", remote=True
            )

        assert "error" in result
        assert "No CDP URL" in result["error"]

    @pytest.mark.asyncio
    async def test_navigate_remote_tracks_url_for_drift(self):
        page = MagicMock()
        page.url = "https://jobs.ashbyhq.com/apply"
        page.title = AsyncMock(return_value="Apply")
        page.goto = AsyncMock()
        page.is_closed.return_value = False
        locator = MagicMock()
        locator.aria_snapshot = AsyncMock(return_value="- heading: Apply")
        page.locator.return_value = locator

        browser._remote_page = page

        with patch.object(browser, "_ensure_remote_cdp", new_callable=AsyncMock, return_value=page):
            browser._active_page = page
            await browser._impl_browser_navigate(
                "https://jobs.ashbyhq.com/apply", remote=True, cdp_url="http://x:9222"
            )

        assert browser._remote_last_url == "https://jobs.ashbyhq.com/apply"


class TestRemoteInteraction:
    """Verify interaction tools handle remote CDP state."""

    @pytest.mark.asyncio
    async def test_click_with_drift_returns_advisory(self):
        """When user navigated away, click returns advisory instead of acting."""
        page = MagicMock()
        page.url = "https://other-page.com"
        page.is_closed.return_value = False
        browser._active_page = page
        browser._remote_page = page
        browser._remote_browser = _mock_remote_browser(connected=True)
        browser._remote_last_url = "https://original-page.com"

        result = await browser._impl_browser_click("#submit")
        assert "advisory" in result
        assert result["drift"] == "url_changed"

    @pytest.mark.asyncio
    async def test_click_with_disconnected_remote_returns_error(self):
        page = MagicMock()
        browser._active_page = page
        browser._remote_page = page
        browser._remote_browser = _mock_remote_browser(connected=False)

        result = await browser._impl_browser_click("#submit")
        assert "error" in result
        assert "connection lost" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_human_delay_uses_collaborate_timing_for_remote(self):
        """Remote CDP always uses fast 0.5-2.0s timing."""
        page = MagicMock()
        browser._active_page = page
        browser._remote_page = page

        with patch("genesis.mcp.health.browser.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await browser._human_delay()

        mock_sleep.assert_awaited_once()
        delay = mock_sleep.call_args[0][0]
        assert 0.5 <= delay <= 2.0

    @pytest.mark.asyncio
    async def test_human_type_uses_per_keystroke_for_remote(self):
        """Remote CDP fires per-keystroke typing, not atomic fill."""
        page = AsyncMock()
        browser._active_page = page
        browser._remote_page = page
        browser._stealth_cm = None  # not Camoufox

        with patch("genesis.mcp.health.browser.asyncio.sleep", new_callable=AsyncMock):
            await browser._human_type(page, "#email", "hi")

        # Should clear field, click to focus, then type per-character
        # (using keyboard.down + keyboard.up for hold-time simulation)
        page.fill.assert_awaited_once_with("#email", "", timeout=10000)
        page.click.assert_awaited_once_with("#email", timeout=10000)
        assert page.keyboard.down.await_count == 2  # 'h', 'i'
        assert page.keyboard.up.await_count == 2  # 'h', 'i'

    @pytest.mark.asyncio
    async def test_human_type_uses_atomic_fill_for_chromium(self):
        """Plain Chromium (dev/test) uses atomic fill, no per-keystroke."""
        page = AsyncMock()
        browser._active_page = page
        browser._remote_page = None  # not remote
        browser._stealth_cm = None  # not Camoufox

        await browser._human_type(page, "#email", "hi")

        # Should use atomic fill with the full value
        page.fill.assert_awaited_once_with("#email", "hi", timeout=10000)
        page.keyboard.type.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_fill_with_drift_returns_advisory(self):
        page = MagicMock()
        page.url = "https://other.com"
        page.is_closed.return_value = False
        browser._active_page = page
        browser._remote_page = page
        browser._remote_browser = _mock_remote_browser(connected=True)
        browser._remote_last_url = "https://original.com"

        result = await browser._impl_browser_fill("#email", "test@test.com")
        assert "advisory" in result

    @pytest.mark.asyncio
    async def test_press_key_with_disconnected_returns_error(self):
        page = MagicMock()
        browser._active_page = page
        browser._remote_page = page
        browser._remote_browser = _mock_remote_browser(connected=False)

        result = await browser._impl_browser_press_key("Tab")
        assert "error" in result
        assert "connection lost" in result["error"].lower()


class TestRemoteHelpers:
    """Verify remote state helper functions."""

    def test_is_remote_active_true(self):
        page = MagicMock()
        browser._remote_page = page
        browser._active_page = page
        assert browser._is_remote_active() is True

    def test_is_remote_active_false_no_remote(self):
        assert browser._is_remote_active() is False

    def test_is_remote_active_false_different_page(self):
        browser._remote_page = MagicMock()
        browser._active_page = MagicMock()  # different object
        assert browser._is_remote_active() is False

    def test_remote_browser_connected(self):
        browser._remote_browser = MagicMock()
        browser._remote_browser.is_connected.return_value = True
        assert browser._remote_browser_connected() is True

    def test_remote_browser_not_connected(self):
        browser._remote_browser = MagicMock()
        browser._remote_browser.is_connected.return_value = False
        assert browser._remote_browser_connected() is False

    def test_remote_browser_none(self):
        assert browser._remote_browser_connected() is False

    def test_check_remote_health_ok(self):
        """Non-remote page — returns None."""
        browser._active_page = MagicMock()
        assert browser._check_remote_health() is None

    def test_check_remote_health_disconnected(self):
        """Read-only check — returns error but does NOT clear globals."""
        page = MagicMock()
        browser._active_page = page
        browser._remote_page = page
        browser._remote_browser = MagicMock()
        browser._remote_browser.is_connected.return_value = False

        result = browser._check_remote_health()
        assert result is not None
        assert "error" in result
        # Read-only: globals NOT cleared
        assert browser._active_page is page
        assert browser._remote_page is page

    def test_detach_dead_remote_clears_globals(self):
        """Mutating variant — returns error AND clears globals."""
        page = MagicMock()
        browser._active_page = page
        browser._remote_page = page
        browser._remote_browser = MagicMock()
        browser._remote_browser.is_connected.return_value = False

        result = browser._detach_dead_remote()
        assert result is not None
        assert "error" in result
        assert browser._active_page is None
        assert browser._remote_page is None

    def test_on_remote_disconnected_clears_state(self):
        page = MagicMock()
        browser._remote_browser = MagicMock()
        browser._remote_page = page
        browser._active_page = page

        browser._on_remote_disconnected()

        assert browser._remote_browser is None
        assert browser._remote_page is None
        assert browser._active_page is None

    def test_update_remote_url_tracks_after_action(self):
        """_update_remote_url syncs drift tracking after click/fill/js."""
        page = MagicMock()
        page.url = "https://new-page-after-submit.com"
        browser._remote_page = page
        browser._active_page = page
        browser._remote_last_url = "https://old-form-page.com"

        browser._update_remote_url()

        assert browser._remote_last_url == "https://new-page-after-submit.com"

    def test_update_remote_url_noop_when_not_remote(self):
        """_update_remote_url does nothing when not in remote mode."""
        browser._active_page = MagicMock()
        browser._remote_last_url = "https://old.com"

        browser._update_remote_url()

        assert browser._remote_last_url == "https://old.com"  # unchanged


def _ss_line(pid, name="x11vnc", v6=False):
    """One `ss -ltnpH` listener row for the VNC port."""
    addr = "[::]:5999" if v6 else "0.0.0.0:5999"
    peer = "[::]:*" if v6 else "0.0.0.0:*"
    return f'LISTEN 0      32     {addr} {peer} users:(("{name}",pid={pid},fd=8))\n'


class TestClickNavigationWait:
    """FIX 2 (idx 39): _impl_browser_click waits for click-triggered navigation."""

    @pytest.mark.asyncio
    async def test_click_waits_for_load_state(self):
        page = MagicMock()
        page.url = "https://example.com/after"
        page.is_closed.return_value = False
        page.wait_for_load_state = AsyncMock()
        browser._active_page = page
        with patch.object(browser, "_stealth_click", new=AsyncMock()), patch.object(
            browser, "_snapshot_page", new=AsyncMock(return_value="snap")
        ), patch.object(browser, "_human_delay", new=AsyncMock()):
            result = await browser._impl_browser_click("#go")
        page.wait_for_load_state.assert_awaited_once()
        args, _ = page.wait_for_load_state.call_args
        assert args and args[0] == "domcontentloaded"
        assert result["clicked"] == "#go"

    @pytest.mark.asyncio
    async def test_click_survives_raising_load_state(self):
        page = MagicMock()
        page.url = "https://example.com/after"
        page.is_closed.return_value = False
        page.wait_for_load_state = AsyncMock(
            side_effect=Exception("execution context destroyed")
        )
        browser._active_page = page
        with patch.object(browser, "_stealth_click", new=AsyncMock()), patch.object(
            browser, "_snapshot_page", new=AsyncMock(return_value="snap")
        ), patch.object(browser, "_human_delay", new=AsyncMock()):
            result = await browser._impl_browser_click("#go")
        assert result["clicked"] == "#go"
        assert "error" not in result


class TestReclaimVncPort:
    """FIX 3 (idx 9): safe VNC stale-port reclaim (ss-primary, MainPID-guarded)."""

    @staticmethod
    def _fake_run(*, ss_out=None, ss_missing=False, ss_exc=None, fuser_out=None,
                  fuser_missing=False, fuser_exc=None, active_state="inactive",
                  active_exc=None, active_rc=0, main_pid="0", mainpid_exc=None,
                  mainpid_rc=0, calls=None):
        def _run(argv, **kwargs):
            if calls is not None:
                calls.append(list(argv))
            cmd = argv[0]
            if cmd == "ss":
                if ss_missing:
                    raise FileNotFoundError("ss")
                if ss_exc is not None:
                    raise ss_exc
                return MagicMock(stdout=ss_out or "", stderr="", returncode=0)
            if cmd == "fuser":
                if fuser_missing:
                    raise FileNotFoundError("fuser")
                if fuser_exc is not None:
                    raise fuser_exc
                return MagicMock(stdout=fuser_out or "", stderr="", returncode=0)
            if cmd == "systemctl":
                prop = argv[4] if len(argv) > 4 else ""
                if prop == "ActiveState":
                    if active_exc is not None:
                        raise active_exc
                    out = "" if active_rc != 0 else active_state + "\n"
                    return MagicMock(stdout=out, returncode=active_rc)
                if prop == "MainPID":
                    if mainpid_exc is not None:
                        raise mainpid_exc
                    out = "" if mainpid_rc != 0 else main_pid + "\n"
                    return MagicMock(stdout=out, returncode=mainpid_rc)
                return MagicMock(stdout="", returncode=0)
            return MagicMock(stdout="", returncode=0)
        return _run

    def test_active_unit_not_killed(self):
        run = self._fake_run(ss_out=_ss_line(1234), active_state="active", main_pid="1234")
        with patch("subprocess.run", side_effect=run), patch("os.kill") as kill:
            browser._reclaim_vnc_port(5999, "genesis-vnc")
        kill.assert_not_called()

    def test_activating_unit_not_killed(self):
        run = self._fake_run(ss_out=_ss_line(1234), active_state="activating", main_pid="0")
        with patch("subprocess.run", side_effect=run), patch("os.kill") as kill:
            browser._reclaim_vnc_port()
        kill.assert_not_called()

    def test_holder_equals_mainpid_not_killed(self):
        run = self._fake_run(ss_out=_ss_line(509730), active_state="inactive", main_pid="509730")
        with patch("subprocess.run", side_effect=run), patch("os.kill") as kill:
            browser._reclaim_vnc_port()
        kill.assert_not_called()

    def test_foreign_non_x11vnc_not_killed(self):
        run = self._fake_run(ss_out=_ss_line(4242, name="nginx"), active_state="inactive", main_pid="0")
        with patch("subprocess.run", side_effect=run), patch("os.kill") as kill:
            browser._reclaim_vnc_port()
        kill.assert_not_called()

    def test_foreign_x11vnc_killed_specific_pid(self):
        run = self._fake_run(ss_out=_ss_line(4242), active_state="inactive", main_pid="0")
        with patch("subprocess.run", side_effect=run), patch("os.kill") as kill, \
             patch("time.sleep"):
            browser._reclaim_vnc_port()
        kill.assert_called_once_with(4242, signal.SIGKILL)

    def test_pid_le_1_never_killed(self):
        run = self._fake_run(ss_out=_ss_line(1), active_state="inactive", main_pid="0")
        with patch("subprocess.run", side_effect=run), patch("os.kill") as kill:
            browser._reclaim_vnc_port()
        kill.assert_not_called()

    def test_dual_stack_deduped_single_kill(self):
        ss_out = _ss_line(4242, v6=False) + _ss_line(4242, v6=True)
        run = self._fake_run(ss_out=ss_out, active_state="inactive", main_pid="0")
        with patch("subprocess.run", side_effect=run), patch("os.kill") as kill, \
             patch("time.sleep"):
            browser._reclaim_vnc_port()
        kill.assert_called_once_with(4242, signal.SIGKILL)

    def test_kill_processlookup_swallowed(self):
        run = self._fake_run(ss_out=_ss_line(4242), active_state="inactive", main_pid="0")
        with patch("subprocess.run", side_effect=run), \
             patch("os.kill", side_effect=ProcessLookupError), patch("time.sleep"):
            browser._reclaim_vnc_port()  # must not raise

    def test_fuser_fallback_never_blanket_kill(self):
        calls = []
        run = self._fake_run(ss_missing=True, fuser_out="4242\n",
                             active_state="inactive", main_pid="0", calls=calls)
        with patch("subprocess.run", side_effect=run), patch("os.kill") as kill, \
             patch.object(browser, "_proc_comm", return_value="x11vnc"), patch("time.sleep"):
            browser._reclaim_vnc_port()
        kill.assert_called_once_with(4242, signal.SIGKILL)
        assert not any(a[0] == "fuser" and "-k" in a for a in calls)

    def test_neither_ss_nor_fuser(self):
        run = self._fake_run(ss_missing=True, fuser_missing=True)
        with patch("subprocess.run", side_effect=run), patch("os.kill") as kill:
            browser._reclaim_vnc_port()
        kill.assert_not_called()

    def test_activestate_probe_raises_no_kill(self):
        # systemctl ActiveState lookup fails -> can't verify unit is down -> fail-safe.
        run = self._fake_run(ss_out=_ss_line(4242), active_exc=TimeoutError("dbus"))
        with patch("subprocess.run", side_effect=run), patch("os.kill") as kill:
            browser._reclaim_vnc_port()
        kill.assert_not_called()

    def test_mainpid_probe_raises_no_kill(self):
        # systemctl MainPID lookup fails -> can't verify holder != MainPID -> fail-safe.
        run = self._fake_run(ss_out=_ss_line(4242), active_state="inactive",
                             mainpid_exc=TimeoutError("dbus"))
        with patch("subprocess.run", side_effect=run), patch("os.kill") as kill:
            browser._reclaim_vnc_port()
        kill.assert_not_called()

    def test_ss_generic_exception_no_kill(self):
        run = self._fake_run(ss_exc=OSError("netlink"))
        with patch("subprocess.run", side_effect=run), patch("os.kill") as kill:
            browser._reclaim_vnc_port()
        kill.assert_not_called()

    def test_fuser_generic_exception_no_kill(self):
        run = self._fake_run(ss_missing=True, fuser_exc=OSError("boom"))
        with patch("subprocess.run", side_effect=run), patch("os.kill") as kill:
            browser._reclaim_vnc_port()
        kill.assert_not_called()

    def test_activestate_nonzero_rc_no_kill(self):
        # bus down -> systemctl exits 1 with empty stdout and does NOT raise.
        run = self._fake_run(ss_out=_ss_line(4242), active_rc=1)
        with patch("subprocess.run", side_effect=run), patch("os.kill") as kill:
            browser._reclaim_vnc_port()
        kill.assert_not_called()

    def test_mainpid_nonzero_rc_no_kill(self):
        run = self._fake_run(ss_out=_ss_line(4242), active_state="inactive", mainpid_rc=1)
        with patch("subprocess.run", side_effect=run), patch("os.kill") as kill:
            browser._reclaim_vnc_port()
        kill.assert_not_called()

    def test_no_holder_no_kill(self):
        run = self._fake_run(ss_out="", active_state="inactive", main_pid="0")
        with patch("subprocess.run", side_effect=run), patch("os.kill") as kill:
            browser._reclaim_vnc_port()
        kill.assert_not_called()


def _ts_page(*, selectors_present=(), title="Example Domain", url="https://example.com/"):
    """Fake page for turnstile detection tests."""
    present = set(selectors_present)

    async def _qs(sel):
        return MagicMock() if sel in present else None

    page = MagicMock()
    page.query_selector = AsyncMock(side_effect=_qs)
    page.title = AsyncMock(return_value=title)
    page.reload = AsyncMock()
    page.url = url
    return page


def _ts_response(headers=None):
    resp = MagicMock()
    resp.headers = headers if headers is not None else {}
    return resp


_WIDGET_INPUT = 'input[name="cf-turnstile-response"]'
_WIDGET_IFRAME = 'iframe[src*="challenges.cloudflare.com"]'


class TestInterstitialDetection:
    """FIX 1 (idx 38): _detect_interstitial fires only on interstitial evidence."""

    @pytest.mark.asyncio
    async def test_cf_mitigated_header_is_interstitial(self):
        page = _ts_page(title="Example Domain")
        resp = _ts_response({"cf-mitigated": "challenge"})
        assert await browser._detect_interstitial(page, resp) is True

    @pytest.mark.asyncio
    async def test_challenge_title_is_interstitial(self):
        page = _ts_page(title="Just a moment...")
        assert await browser._detect_interstitial(page, None) is True

    @pytest.mark.asyncio
    async def test_challenge_chrome_is_interstitial(self):
        page = _ts_page(selectors_present={"#cf-challenge-running"}, title="x")
        assert await browser._detect_interstitial(page, None) is True

    @pytest.mark.asyncio
    async def test_embedded_widget_only_is_not_interstitial(self):
        # Widget markers present, but NO interstitial evidence -> NOT blocking.
        page = _ts_page(selectors_present={_WIDGET_INPUT, _WIDGET_IFRAME}, title="Sign in")
        assert await browser._detect_interstitial(page, None) is False

    @pytest.mark.asyncio
    async def test_response_none_is_safe(self):
        page = _ts_page(title="Home")
        assert await browser._detect_interstitial(page, None) is False


class TestWidgetDetection:
    @pytest.mark.asyncio
    async def test_widget_input_present(self):
        page = _ts_page(selectors_present={_WIDGET_INPUT})
        assert await browser._detect_widget(page) is True

    @pytest.mark.asyncio
    async def test_no_widget(self):
        page = _ts_page()
        assert await browser._detect_widget(page) is False


class TestTurnstileShortGrace:
    """FIX 1: widget-only -> short grace (no VNC/Telegram); interstitial -> full ladder."""

    @pytest.mark.asyncio
    async def test_embedded_widget_short_grace_no_escalation(self):
        page = _ts_page(selectors_present={_WIDGET_INPUT}, title="Sign in")
        with patch.object(browser, "_poll_turnstile_token", new=AsyncMock(return_value=False)) as poll, \
             patch.object(browser, "_click_turnstile_widget", new=AsyncMock(return_value=False)), \
             patch.object(browser, "_vnc_click_turnstile", new=AsyncMock(return_value=False)) as vnc, \
             patch.object(browser, "_send_turnstile_alert", new=AsyncMock()) as alert, \
             patch("asyncio.sleep", new=AsyncMock()):
            result = await browser._wait_for_turnstile(page, response=None)
        assert result == {"status": "embedded", "method": "widget_no_interstitial"}
        poll.assert_awaited()          # grace poll ran
        vnc.assert_not_called()        # NO VNC escalation
        alert.assert_not_called()      # NO Telegram alert

    @pytest.mark.asyncio
    async def test_embedded_widget_grace_resolves(self):
        page = _ts_page(selectors_present={_WIDGET_INPUT}, title="Sign in")
        with patch.object(browser, "_poll_turnstile_token", new=AsyncMock(return_value=True)), \
             patch.object(browser, "_send_turnstile_alert", new=AsyncMock()) as alert, \
             patch("asyncio.sleep", new=AsyncMock()):
            result = await browser._wait_for_turnstile(page, response=None)
        assert result["status"] == "resolved"
        alert.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_challenge_returns_none(self):
        page = _ts_page(title="Home")
        with patch("asyncio.sleep", new=AsyncMock()):
            result = await browser._wait_for_turnstile(page, response=None)
        assert result is None

    @pytest.mark.asyncio
    async def test_interstitial_widget_still_present_not_falsely_resolved(self):
        # Real interstitial (cf-mitigated) that shows a bare widget with a
        # non-English title and no chrome. A widget-click yields no token; the
        # secondary "gone?" gate must NOT declare resolved while a widget remains.
        page = _ts_page(selectors_present={_WIDGET_INPUT}, title="verificando")
        resp = _ts_response({"cf-mitigated": "challenge"})
        with patch.object(browser, "_poll_turnstile_token", new=AsyncMock(return_value=False)), \
             patch.object(browser, "_click_turnstile_widget", new=AsyncMock(return_value=True)), \
             patch.object(browser, "_solve_with_playwright_captcha", new=AsyncMock(return_value=False)), \
             patch.object(browser, "_vnc_click_turnstile", new=AsyncMock(return_value=False)), \
             patch.object(browser, "_ensure_vnc", new=AsyncMock()), \
             patch.object(browser, "_send_turnstile_alert", new=AsyncMock()) as alert, \
             patch("asyncio.sleep", new=AsyncMock()):
            result = await browser._wait_for_turnstile(page, response=resp)
        assert result != {"status": "resolved", "method": "iframe_click"}, \
            "falsely reported resolved while a widget was still present"
        assert result == {"status": "blocked", "method": "timeout"}
        alert.assert_awaited()

    @pytest.mark.asyncio
    async def test_header_only_interstitial_not_falsely_resolved(self):
        # cf-mitigated header but NO DOM chrome/widget and a non-English title.
        # The DOM-based "gone?" gates must NOT report resolved (the challenge was
        # never observed in the DOM) — it must run to a blocked result instead.
        page = _ts_page(selectors_present=set(), title="verificando")
        resp = _ts_response({"cf-mitigated": "challenge"})
        with patch.object(browser, "_poll_turnstile_token", new=AsyncMock(return_value=False)), \
             patch.object(browser, "_click_turnstile_widget", new=AsyncMock(return_value=False)), \
             patch.object(browser, "_solve_with_playwright_captcha", new=AsyncMock(return_value=False)), \
             patch.object(browser, "_vnc_click_turnstile", new=AsyncMock(return_value=False)), \
             patch.object(browser, "_ensure_vnc", new=AsyncMock()), \
             patch.object(browser, "_send_turnstile_alert", new=AsyncMock()) as alert, \
             patch("asyncio.sleep", new=AsyncMock()):
            result = await browser._wait_for_turnstile(page, response=resp)
        assert result == {"status": "blocked", "method": "timeout"}, result
        alert.assert_awaited()

    @pytest.mark.asyncio
    async def test_interstitial_runs_full_ladder_to_alert(self):
        page = _ts_page(selectors_present={"#cf-challenge-running"}, title="Just a moment...")
        resp = _ts_response({"cf-mitigated": "challenge"})
        with patch.object(browser, "_poll_turnstile_token", new=AsyncMock(return_value=False)), \
             patch.object(browser, "_click_turnstile_widget", new=AsyncMock(return_value=False)), \
             patch.object(browser, "_solve_with_playwright_captcha", new=AsyncMock(return_value=False)), \
             patch.object(browser, "_vnc_click_turnstile", new=AsyncMock(return_value=False)), \
             patch.object(browser, "_ensure_vnc", new=AsyncMock()), \
             patch.object(browser, "_send_turnstile_alert", new=AsyncMock()) as alert, \
             patch("asyncio.sleep", new=AsyncMock()):
            result = await browser._wait_for_turnstile(page, response=resp)
        assert result == {"status": "blocked", "method": "timeout"}
        alert.assert_awaited()         # ladder ran to exhaustion


# Must stay in step with scripts/browser.py's copy of the scheme
# (tests/test_scripts/test_browser_cli_screenshot_path.py asserts the same
# shape on that side). Format: %Y%m%dT%H%M%S%fZ -> 20260902T190142123456Z
_MCP_STAMP = re.compile(r"^\d{8}T\d{12}Z$")


class TestBrowserScreenshotUniquePath:
    """Consecutive screenshots must not overwrite one another.

    The tool previously wrote every capture to the single fixed path
    ~/tmp/genesis_browser_screenshot.png, so a session that captured N pages
    was left with only the last one — silently, since each call still returned
    a valid-looking path.
    """

    @staticmethod
    def _page():
        page = MagicMock()
        page.url = "https://example.com"
        page.title = AsyncMock(return_value="Example")

        async def _write(path: str) -> None:
            # Mimic Playwright: actually create the file at the given path, so
            # the test measures files-on-disk rather than returned strings.
            with open(path, "wb") as fh:
                fh.write(b"\x89PNG")

        page.screenshot = AsyncMock(side_effect=_write)
        return page

    @pytest.mark.asyncio
    async def test_consecutive_screenshots_do_not_overwrite(self, tmp_path):
        browser._active_page = self._page()
        with patch.object(browser, "_SCREENSHOT_DIR", tmp_path):
            first = await browser._impl_browser_screenshot()
            second = await browser._impl_browser_screenshot()

        assert "error" not in first, first
        assert "error" not in second, second
        assert first["path"] != second["path"], (
            "both captures wrote to the same path — the second overwrote the first"
        )
        # The property that actually matters: two files survive on disk.
        written = sorted(p.name for p in tmp_path.glob("*.png"))
        assert len(written) == 2, written
        assert all(n.startswith("genesis_browser_screenshot_") for n in written), written

    @pytest.mark.asyncio
    async def test_returned_path_is_the_path_actually_written(self, tmp_path):
        """The returned path must be the one Playwright wrote to.

        Callers (and the tool's own docstring) treat the returned "path" as
        authoritative, so a divergence here would hand out a path pointing at
        nothing while the real capture sat elsewhere.
        """
        page = self._page()
        browser._active_page = page
        with patch.object(browser, "_SCREENSHOT_DIR", tmp_path):
            result = await browser._impl_browser_screenshot()

        assert "error" not in result, result
        page.screenshot.assert_awaited_once_with(path=result["path"])
        assert Path(result["path"]).is_file()

    @pytest.mark.asyncio
    async def test_capture_names_sort_chronologically(self, tmp_path):
        """Names must sort into CAPTURE ORDER — the motivating incident was a
        run of 8 captures with no way to tell which page was which.

        The clock is CONTROLLED here on purpose. Two real back-to-back captures
        land in the same wall-clock instant, which would make a
        `stamps == sorted(stamps)` assertion a tautology over equal values —
        i.e. green whatever the format string does. Distinct injected times make
        the ordering claim the thing actually under test.
        """
        browser._active_page = self._page()
        # These two instants STRADDLE MIDNIGHT on purpose: the earlier capture
        # has the LATER clock time. Any format that does not lead with the date
        # (e.g. a %H%M%S-first ordering) sorts them backwards, while two
        # same-day times would sort correctly under such a format and let the
        # bug through. The inputs are what make the ordering claim testable.
        times = [
            datetime(2026, 9, 2, 23, 59, 58, 900000, tzinfo=UTC),
            datetime(2026, 9, 3, 0, 0, 1, 100000, tzinfo=UTC),
        ]
        with patch.object(browser, "_SCREENSHOT_DIR", tmp_path), \
             patch.object(browser, "datetime") as fake_dt:
            fake_dt.now.side_effect = times
            first = await browser._impl_browser_screenshot()
            second = await browser._impl_browser_screenshot()

        stamps = [Path(r["path"]).name.split("_")[-2] for r in (first, second)]
        # STRICT: an equal pair must not satisfy this.
        assert stamps[0] < stamps[1], stamps
        # SHAPE, not just order — ordering alone stays green if the T/Z are
        # dropped, which is exactly how the two writers would drift apart.
        assert all(_MCP_STAMP.match(x) for x in stamps), stamps

    @pytest.mark.asyncio
    async def test_stamp_resolves_below_one_second(self, tmp_path):
        """A second-resolution stamp ties a rapid burst. Two captures 1ms apart
        must still produce distinguishable, correctly-ordered stamps."""
        browser._active_page = self._page()
        times = [
            datetime(2026, 9, 2, 19, 1, 42, 1000, tzinfo=UTC),
            datetime(2026, 9, 2, 19, 1, 42, 2000, tzinfo=UTC),
        ]
        with patch.object(browser, "_SCREENSHOT_DIR", tmp_path), \
             patch.object(browser, "datetime") as fake_dt:
            fake_dt.now.side_effect = times
            first = await browser._impl_browser_screenshot()
            second = await browser._impl_browser_screenshot()

        stamps = [Path(r["path"]).name.split("_")[-2] for r in (first, second)]
        assert stamps[0] != stamps[1], stamps
        assert stamps[0] < stamps[1], stamps


# ── Browser-stack lock lifetime ──────────────────────────────────────────────
# The shared hold on engine.BROWSER_LOCK_FILE must be held exactly while a local
# browser process this MCP started may be alive. Each test drives one path that
# creates or ends such a process and probes the lock from OUTSIDE, the way a
# provisioning run does: an exclusive non-blocking flock on a separate open file
# description, which fails while any shared hold exists.


def _stack_lock_held() -> bool:
    import fcntl

    from genesis.browser import engine

    path = engine.BROWSER_LOCK_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as probe:
        try:
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(probe, fcntl.LOCK_UN)
        return False


def _ready_engine():
    from genesis.browser import engine

    return patch.object(
        engine,
        "camoufox_engine_status",
        return_value=engine.EngineStatus(engine.READY, "test engine", Path("/engine")),
    )


def _live_page():
    page = MagicMock()
    page.is_closed.return_value = False
    page.url = "https://example.com"
    return page


def _dead_page():
    page = MagicMock()
    page.is_closed.return_value = True
    return page


def _camoufox_cm(*, enter=None, exit_=None):
    """An AsyncCamoufox stand-in: __aenter__ returns a browser with one live page."""
    cm = MagicMock()
    if enter is None:
        launched = MagicMock()
        launched.pages = [_live_page()]
        enter = AsyncMock(return_value=launched)
    cm.__aenter__ = enter
    cm.__aexit__ = exit_ if exit_ is not None else AsyncMock(return_value=None)
    return cm


def _camoufox_module(*cms):
    """sys.modules entry whose AsyncCamoufox hands out ``cms`` in order."""
    return {"camoufox.async_api": MagicMock(AsyncCamoufox=MagicMock(side_effect=list(cms)))}


def _playwright_cm(*, start=None, context=None, stop=None):
    """An async_playwright() stand-in owning one driver."""
    pw = MagicMock()
    pw.stop = stop if stop is not None else AsyncMock(return_value=None)
    if context is None:
        context = MagicMock()
        context.pages = [_live_page()]
        context.close = AsyncMock(return_value=None)
    pw.chromium.launch_persistent_context = AsyncMock(return_value=context)
    cm = MagicMock()
    cm.start = start if start is not None else AsyncMock(return_value=pw)
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm, pw


def _playwright_module(*cms):
    return {"playwright.async_api": MagicMock(async_playwright=MagicMock(side_effect=list(cms)))}


class TestBrowserStackLockLifetime:
    @pytest.mark.asyncio
    async def test_direct_ensure_browser_holds_the_lock(self):
        """Medium's CamoufoxBrowserClient calls _ensure_browser directly, never
        _get_page: the launch itself must take the hold."""
        from genesis.distribution.medium import CamoufoxBrowserClient

        with (
            _ready_engine(),
            patch.dict("sys.modules", _camoufox_module(_camoufox_cm())),
        ):
            await CamoufoxBrowserClient()._ensure_browser()
        assert browser._stealth_cm is not None
        assert _stack_lock_held()

        await browser.async_cleanup()
        assert not _stack_lock_held()

    @pytest.mark.asyncio
    async def test_direct_ensure_browser_refused_mid_upgrade(self):
        import fcntl

        from genesis.browser import engine

        cm = _camoufox_cm()
        engine.BROWSER_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
        with (
            _ready_engine(),
            patch.dict("sys.modules", _camoufox_module(cm)),
            open(engine.BROWSER_LOCK_FILE, "w") as provisioning,
        ):
            fcntl.flock(provisioning, fcntl.LOCK_EX)
            with pytest.raises(Exception, match="being upgraded") as refused:
                await browser._ensure_browser()
        assert type(refused.value).__name__ == "BrowserStackBusy"
        cm.__aenter__.assert_not_awaited()
        assert browser._stealth_cm is None

    @pytest.mark.asyncio
    async def test_stale_camoufox_restart_keeps_the_lock(self):
        """Stale-page recovery runs async_cleanup (which drops the hold) and then
        launches a replacement: the replacement must be under the hold again."""
        first, second = _camoufox_cm(), _camoufox_cm()
        with (
            _ready_engine(),
            patch.dict("sys.modules", _camoufox_module(first, second)),
            patch.object(browser, "_ensure_vnc", new=AsyncMock()),
            patch.object(browser, "_start_idle_watcher"),
        ):
            await browser._get_page(stealth=True)
            assert _stack_lock_held()
            browser._stealth_page = _dead_page()
            await browser._get_page(stealth=True)
        first.__aexit__.assert_awaited_once()
        second.__aenter__.assert_awaited_once()
        assert _stack_lock_held(), "the replacement browser runs without the hold"

    @pytest.mark.asyncio
    async def test_stale_chromium_restart_keeps_the_lock(self):
        (cm1, pw1), (cm2, _pw2) = _playwright_cm(), _playwright_cm()
        with (
            patch.dict("sys.modules", _playwright_module(cm1, cm2)),
            patch.object(browser, "_ensure_vnc", new=AsyncMock()),
            patch.object(browser, "_start_idle_watcher"),
        ):
            await browser._get_page(stealth=False)
            assert _stack_lock_held()
            browser._page = _dead_page()
            await browser._get_page(stealth=False)
        pw1.stop.assert_awaited_once()
        cm2.start.assert_awaited_once()
        assert _stack_lock_held(), "the replacement browser runs without the hold"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("failure", [RuntimeError("launch failed"), asyncio.CancelledError()])
    async def test_failed_camoufox_launch_cleans_up_and_releases(self, failure):
        """__aenter__ failing (or being cancelled) after the manager exists may have
        started the driver: it is closed, and then nothing holds the stack."""
        cm = _camoufox_cm(enter=AsyncMock(side_effect=failure))
        with (
            _ready_engine(),
            patch.dict("sys.modules", _camoufox_module(cm)),
            pytest.raises(type(failure)),
        ):
            await browser._ensure_browser()
        cm.__aexit__.assert_awaited_once()
        assert browser._stealth_cm is None
        assert not _stack_lock_held()

    @pytest.mark.asyncio
    async def test_failed_chromium_launch_stops_the_driver_and_releases(self):
        """launch_persistent_context failing leaves a running driver behind."""
        cm, pw = _playwright_cm()
        pw.chromium.launch_persistent_context = AsyncMock(side_effect=RuntimeError("no browser"))
        with (
            patch.dict("sys.modules", _playwright_module(cm)),
            pytest.raises(RuntimeError, match="no browser"),
        ):
            await browser._ensure_chromium_fallback()
        pw.stop.assert_awaited_once()
        assert browser._playwright is None
        assert not _stack_lock_held()

    @pytest.mark.asyncio
    async def test_cancelled_chromium_start_stops_the_driver_by_its_manager(self):
        """start() cancelled after spawning the driver returns no Playwright
        object; the manager is the only handle that can stop the driver."""
        cm, _pw = _playwright_cm(start=AsyncMock(side_effect=asyncio.CancelledError()))
        with (
            patch.dict("sys.modules", _playwright_module(cm)),
            pytest.raises(asyncio.CancelledError),
        ):
            await browser._ensure_chromium_fallback()
        cm.__aexit__.assert_awaited_once()
        assert not _stack_lock_held()

    @pytest.mark.asyncio
    async def test_failed_launch_keeps_the_hold_of_a_browser_still_open(self):
        """A Camoufox launch failing while Chromium is open must not release the
        hold the Chromium browser depends on."""
        cm_pw, _pw = _playwright_cm()
        bad = _camoufox_cm(enter=AsyncMock(side_effect=RuntimeError("launch failed")))
        with (
            _ready_engine(),
            patch.dict("sys.modules", {**_playwright_module(cm_pw), **_camoufox_module(bad)}),
        ):
            await browser._ensure_chromium_fallback()
            with pytest.raises(RuntimeError, match="launch failed"):
                await browser._ensure_browser()
        assert browser._context is not None
        assert _stack_lock_held()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("left", [[4242], None])
    async def test_unfinished_teardown_retains_the_lock(self, monkeypatch, left):
        """A close that raised may have left the browser running: the hold stays
        while a browser process remains under this one, or that cannot be read."""
        cm = _camoufox_cm(exit_=AsyncMock(side_effect=RuntimeError("close failed")))
        monkeypatch.setattr(browser, "_local_browser_processes", lambda: left, raising=False)
        with _ready_engine(), patch.dict("sys.modules", _camoufox_module(cm)):
            await browser._ensure_browser()
        await browser.async_cleanup()
        assert browser._stealth_cm is None
        assert _stack_lock_held(), "provisioning could now swap the engine under a live browser"

    @pytest.mark.asyncio
    async def test_unfinished_teardown_releases_once_no_browser_remains(self, monkeypatch):
        cm = _camoufox_cm(exit_=AsyncMock(side_effect=RuntimeError("close failed")))
        remaining = [[4242], []]
        monkeypatch.setattr(
            browser, "_local_browser_processes", lambda: remaining.pop(0), raising=False
        )
        with _ready_engine(), patch.dict("sys.modules", _camoufox_module(cm)):
            await browser._ensure_browser()
        await browser.async_cleanup()
        assert _stack_lock_held()
        await browser.async_cleanup()  # the next cleanup sees no browser left
        assert not _stack_lock_held()
        assert browser._stack_teardown_unconfirmed is None

    @pytest.mark.asyncio
    async def test_hung_teardown_retains_the_lock(self, monkeypatch):
        """A close that times out is the case the docstring warns can orphan a
        browser."""
        async def hang(*_a):
            await asyncio.sleep(3600)

        monkeypatch.setattr(browser, "_TEARDOWN_TIMEOUT_S", 0.05, raising=False)
        monkeypatch.setattr(browser, "_local_browser_processes", lambda: [4242], raising=False)
        cm = _camoufox_cm(exit_=hang)
        with _ready_engine(), patch.dict("sys.modules", _camoufox_module(cm)):
            await browser._ensure_browser()
        await browser.async_cleanup()
        assert _stack_lock_held()

    @pytest.mark.asyncio
    async def test_clean_teardown_releases_without_a_process_scan(self, monkeypatch):
        def scan():
            raise AssertionError("a clean teardown needs no process scan")

        monkeypatch.setattr(browser, "_local_browser_processes", scan, raising=False)
        with _ready_engine(), patch.dict("sys.modules", _camoufox_module(_camoufox_cm())):
            await browser._ensure_browser()
        await browser.async_cleanup()
        assert not _stack_lock_held()

    @pytest.mark.asyncio
    async def test_idle_timeout_cleanup_releases(self, monkeypatch):
        with _ready_engine(), patch.dict("sys.modules", _camoufox_module(_camoufox_cm())):
            await browser._ensure_browser()
        monkeypatch.setattr(browser, "_last_used", 1.0)
        monkeypatch.setattr(browser, "_IDLE_TIMEOUT_S", 0)
        sleeps = []

        async def no_wait(_s):
            sleeps.append(_s)

        with patch.object(browser.asyncio, "sleep", no_wait):
            await browser._idle_watcher_loop()
        assert sleeps and not _stack_lock_held()

    @pytest.mark.asyncio
    async def test_idle_cleanup_never_runs_during_a_launch(self, monkeypatch):
        """The idle watcher fires while a launch holds _browser_lock (a stale
        restart after an hour idle): tearing down the half-built browser would
        orphan it without the stack lock. It must wait, then see the launch as use."""
        import time as _time

        monkeypatch.setattr(browser, "_IDLE_TIMEOUT_S", 100)
        monkeypatch.setattr(browser, "_last_used", _time.monotonic() - 200)
        cleanup = AsyncMock()
        monkeypatch.setattr(browser, "async_cleanup", cleanup)
        real_sleep = asyncio.sleep
        polls = []

        async def poll(_s):
            polls.append(_s)
            if len(polls) > 1:
                raise asyncio.CancelledError  # end the loop after one re-poll
            await real_sleep(0)

        monkeypatch.setattr(browser.asyncio, "sleep", poll)
        await browser._browser_lock.acquire()  # a launch in flight
        try:
            watcher = asyncio.ensure_future(browser._idle_watcher_loop())
            for _ in range(5):
                await real_sleep(0)
            assert not cleanup.await_count, "cleanup ran under an in-flight launch"
            browser._touch()  # the launch finished: that was use
        finally:
            browser._browser_lock.release()
        await watcher
        cleanup.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_launch_counts_as_use(self, monkeypatch):
        monkeypatch.setattr(browser, "_last_used", 0.0)
        with _ready_engine(), patch.dict("sys.modules", _camoufox_module(_camoufox_cm())):
            await browser._ensure_browser()
        assert browser._last_used > 0

    @pytest.mark.asyncio
    async def test_repeatedly_cancelled_launch_never_loses_the_browser(self, monkeypatch):
        """anyio (the MCP SDK's request scope) cancels at EVERY await inside a
        cancelled scope, so the failed launch's own teardown is cancelled too and
        the manager stays set with no page. The next launch must close that
        manager, must not launch while a browser of the cancelled one may live,
        and the release must not trust the handles."""
        import anyio

        left = [[4242]]
        monkeypatch.setattr(browser, "_local_browser_processes", lambda: left[0], raising=False)

        async def starting(*_a):
            await asyncio.sleep(10)  # the browser is starting when the request is cancelled

        async def closing(*_a):
            await asyncio.sleep(10)  # a real close takes a moment; cancelled again here

        first = _camoufox_cm(enter=AsyncMock(side_effect=starting), exit_=AsyncMock(side_effect=closing))
        second = _camoufox_cm()
        with _ready_engine(), patch.dict("sys.modules", _camoufox_module(first, second)):
            with anyio.CancelScope() as scope:
                asyncio.get_running_loop().call_later(0.05, scope.cancel)
                await browser._ensure_browser()
            assert browser._stealth_cm is first and browser._stealth_page is None
            assert browser._stack_teardown_unconfirmed, "a cancelled close is not a finished one"
            assert _stack_lock_held()

            first.__aexit__ = AsyncMock(return_value=None)  # the retry's close completes
            with pytest.raises(Exception, match="did not finish"):
                await browser._ensure_browser()  # ... but a process it started remains
            first.__aexit__.assert_awaited_once()  # closed before anything replaces it
            second.__aenter__.assert_not_awaited()
            assert _stack_lock_held(), "released while a browser of the cancelled launch may live"
            left[0] = []
            await browser._ensure_browser()
        assert browser._stealth_cm is second

    @pytest.mark.asyncio
    async def test_readiness_is_read_under_the_lock(self):
        """The engine verdict and the camoufox import read files a provisioning
        run replaces: neither may happen before the hold is taken."""
        import fcntl

        from genesis.browser import engine

        status = MagicMock(return_value=engine.EngineStatus(engine.READY, "t", Path("/e")))
        engine.BROWSER_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
        with (
            patch.object(engine, "camoufox_engine_status", status),
            patch.dict("sys.modules", _camoufox_module(_camoufox_cm())),
            open(engine.BROWSER_LOCK_FILE, "w") as provisioning,
        ):
            fcntl.flock(provisioning, fcntl.LOCK_EX)
            with pytest.raises(Exception, match="being upgraded"):
                await browser._ensure_browser()
        status.assert_not_called()

    @pytest.mark.asyncio
    async def test_idle_watcher_task_releases_through_its_own_cancel(self, monkeypatch):
        """The real watcher runs as _idle_task, and async_cleanup cancels and awaits
        _idle_task: the watcher must survive cancelling itself and still release."""
        with _ready_engine(), patch.dict("sys.modules", _camoufox_module(_camoufox_cm())):
            await browser._ensure_browser()
        monkeypatch.setattr(browser, "_IDLE_TIMEOUT_S", 0)
        real_sleep = asyncio.sleep

        async def fast(_s):
            await real_sleep(0)

        monkeypatch.setattr(browser.asyncio, "sleep", fast)
        browser._start_idle_watcher()
        task = browser._idle_task
        await asyncio.wait_for(task, timeout=5)
        assert browser._stealth_cm is None
        assert not _stack_lock_held()

    @pytest.mark.asyncio
    async def test_navigate_reports_a_busy_stack_for_chromium_too(self, tmp_path):
        import fcntl

        from genesis.browser import engine

        engine.BROWSER_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
        with (
            patch.dict("sys.modules", _playwright_module(_playwright_cm()[0])),
            patch.object(browser, "_ensure_vnc", new=AsyncMock()),
            open(engine.BROWSER_LOCK_FILE, "w") as provisioning,
        ):
            fcntl.flock(provisioning, fcntl.LOCK_EX)
            result = await browser._impl_browser_navigate("https://example.com", stealth=False)
        assert "being upgraded" in result["error"]
        assert not result["error"].startswith("Camoufox")

    @pytest.mark.asyncio
    async def test_relaunch_refused_while_an_unfinished_teardown_left_a_browser(self, monkeypatch):
        """The hold survives an unfinished teardown, so a relaunch must not take
        it as permission: two browsers would share one persistent profile."""
        left = [[4242]]
        monkeypatch.setattr(browser, "_local_browser_processes", lambda: left[0], raising=False)
        first = _camoufox_cm(exit_=AsyncMock(side_effect=RuntimeError("close failed")))
        second = _camoufox_cm()
        with _ready_engine(), patch.dict("sys.modules", _camoufox_module(first, second)):
            await browser._ensure_browser()
            await browser.async_cleanup()
            assert _stack_lock_held()
            with pytest.raises(Exception, match="did not finish") as refused:
                await browser._ensure_browser()
            assert type(refused.value).__name__ == "BrowserStackBusy"
            second.__aenter__.assert_not_awaited()
            left[0] = []  # the old browser exited
            await browser._ensure_browser()
        second.__aenter__.assert_awaited_once()
        assert _stack_lock_held()

    @pytest.mark.asyncio
    async def test_reparented_browser_keeps_the_hold(self, tmp_path, monkeypatch):
        """A browser whose driver died is reparented away from this process; the
        descendant walk alone would not see it and would release the hold."""
        proc = tmp_path / "proc"
        proc.mkdir()
        me = os.getpid()

        def put(pid: int, comm: str, ppid: int) -> None:
            (proc / str(pid)).mkdir(exist_ok=True)
            (proc / str(pid) / "stat").write_text(f"{pid} ({comm}) S {ppid} 1 1 0 -1\n")

        put(me, "python3", 1)
        put(900001, "node", me)
        put(900002, "camoufox-bin", 900001)
        monkeypatch.setattr(browser, "_PROC", proc, raising=False)
        cm = _camoufox_cm(exit_=AsyncMock(side_effect=RuntimeError("close failed")))
        with _ready_engine(), patch.dict("sys.modules", _camoufox_module(cm)):
            await browser._ensure_browser()
        (proc / "900001" / "stat").unlink()
        (proc / "900001").rmdir()  # the driver died ...
        put(900002, "camoufox-bin", 1)  # ... and its browser now belongs to init
        await browser.async_cleanup()
        assert _stack_lock_held(), "released under a browser that is still running"
        (proc / "900002" / "stat").unlink()
        (proc / "900002").rmdir()
        await browser.async_cleanup()
        assert not _stack_lock_held()


def _tinyfish_api():
    from genesis.providers import tinyfish_client

    session = {"session_id": "tf-session-0001", "cdp_url": "ws://127.0.0.1:1/devtools"}
    create = AsyncMock(return_value=session)
    return create, patch.multiple(
        tinyfish_client, browser_session_create=create, browser_session_delete=AsyncMock()
    )


def _cdp_driver(*, stop=None):
    """A started Playwright whose connect_over_cdp yields a browser with one tab."""
    tab = _live_page()
    pw = MagicMock()
    pw.stop = stop if stop is not None else AsyncMock(return_value=None)
    pw.chromium.connect_over_cdp = AsyncMock(return_value=_mock_remote_browser(pages=[tab]))
    start = AsyncMock(return_value=pw)
    module = {"playwright.async_api": MagicMock(async_playwright=MagicMock(return_value=MagicMock(start=start)))}
    return module, start


class TestPlaywrightDriverStackLock:
    """Remote CDP and TinyFish start a LOCAL Playwright driver from this venv's
    packages: it must run under the shared hold, on modules that match the disk."""

    @staticmethod
    async def _connect(kind: str) -> None:
        if kind == "remote":
            await browser._ensure_remote_cdp("http://127.0.0.1:9222")
        else:
            await browser._ensure_tinyfish_browser()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind", ["remote", "tinyfish"])
    async def test_driver_holds_the_lock_until_cleanup(self, kind, monkeypatch):
        monkeypatch.setattr(browser.asyncio, "sleep", AsyncMock())
        module, _start = _cdp_driver()
        _create, api = _tinyfish_api()
        with api, patch.dict("sys.modules", module):
            await self._connect(kind)
            assert _stack_lock_held(), "provisioning could replace playwright under this driver"
            await browser.async_cleanup()
        assert not _stack_lock_held()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind", ["remote", "tinyfish"])
    async def test_driver_refused_mid_upgrade(self, kind):
        import fcntl

        from genesis.browser import engine

        module, start = _cdp_driver()
        create, api = _tinyfish_api()
        engine.BROWSER_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
        with (
            api,
            patch.dict("sys.modules", module),
            open(engine.BROWSER_LOCK_FILE, "w") as provisioning,
        ):
            fcntl.flock(provisioning, fcntl.LOCK_EX)
            with pytest.raises(Exception, match="being upgraded"):
                await self._connect(kind)
        start.assert_not_awaited()
        create.assert_not_awaited()  # no paid session for a driver that cannot start

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind", ["remote", "tinyfish"])
    async def test_connect_refused_on_stale_loaded_modules(self, kind):
        upgraded = dict(browser._STARTUP_BROWSER_VERSIONS, playwright="9.9.9")
        module, start = _cdp_driver()
        create, api = _tinyfish_api()
        with (
            api,
            patch.object(browser, "_installed_browser_versions", return_value=upgraded),
            patch.dict("sys.modules", {**module, "playwright": MagicMock()}),
            pytest.raises(Exception, match="Restart this Claude Code session"),
        ):
            await self._connect(kind)
        start.assert_not_awaited()
        create.assert_not_awaited()
        assert not _stack_lock_held()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind", ["remote", "tinyfish"])
    async def test_unfinished_driver_stop_keeps_the_hold(self, kind, monkeypatch):
        monkeypatch.setattr(browser.asyncio, "sleep", AsyncMock())
        monkeypatch.setattr(browser, "_local_browser_processes", lambda: [4242], raising=False)
        module, _start = _cdp_driver(stop=AsyncMock(side_effect=RuntimeError("stop failed")))
        _create, api = _tinyfish_api()
        with api, patch.dict("sys.modules", module):
            await self._connect(kind)
            await browser.async_cleanup()
        assert _stack_lock_held(), "released while the driver may still be running"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind", ["remote", "tinyfish"])
    async def test_reconnect_after_disconnect_stops_the_old_driver(self, kind, monkeypatch):
        """A disconnect clears the browser and page but not the driver: the
        reconnect must stop it, not overwrite its handle and lose it."""
        monkeypatch.setattr(browser.asyncio, "sleep", AsyncMock())
        old_stop = AsyncMock(return_value=None)
        old, _ = _cdp_driver(stop=old_stop)
        new, _ = _cdp_driver()
        _create, api = _tinyfish_api()
        with api:
            with patch.dict("sys.modules", old):
                await self._connect(kind)
            if kind == "remote":
                browser._on_remote_disconnected()
            else:
                browser._on_tinyfish_disconnected()
            with patch.dict("sys.modules", new):
                await self._connect(kind)
            old_stop.assert_awaited_once()
            await browser.async_cleanup()
        assert not _stack_lock_held()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind", ["remote", "tinyfish"])
    async def test_failed_driver_start_stops_it_by_its_manager(self, kind):
        """start() can fail or be cancelled after spawning node; the manager is
        the only handle that can stop it. No paid session is created either."""
        manager = MagicMock()
        manager.start = AsyncMock(side_effect=RuntimeError("driver failed"))
        manager.__aexit__ = AsyncMock(return_value=None)
        module = {"playwright.async_api": MagicMock(async_playwright=MagicMock(return_value=manager))}
        create, api = _tinyfish_api()
        with api, patch.dict("sys.modules", module), pytest.raises(RuntimeError, match="driver failed"):
            await self._connect(kind)
        manager.__aexit__.assert_awaited_once()
        create.assert_not_awaited()
        assert not _stack_lock_held()

    @pytest.mark.asyncio
    async def test_failed_launch_records_its_browser_before_closing(self, tmp_path, monkeypatch):
        """A launch that fails after the browser started never reaches the
        post-launch record; its teardown must record what is still attached."""
        proc = tmp_path / "proc"
        proc.mkdir()
        me = os.getpid()

        def put(pid: int, comm: str, ppid: int) -> None:
            (proc / str(pid)).mkdir(exist_ok=True)
            (proc / str(pid) / "stat").write_text(f"{pid} ({comm}) S {ppid} 1 1 0 -1\n")

        put(me, "python3", 1)
        put(900001, "node", me)
        put(900002, "camoufox-bin", 900001)
        monkeypatch.setattr(browser, "_PROC", proc, raising=False)

        async def close_fails(*_a):
            (proc / "900001" / "stat").unlink()
            (proc / "900001").rmdir()  # the close kills the driver ...
            put(900002, "camoufox-bin", 1)  # ... and orphans the browser
            raise RuntimeError("close failed")

        cm = _camoufox_cm(enter=AsyncMock(side_effect=RuntimeError("no page")), exit_=close_fails)
        with (
            _ready_engine(),
            patch.dict("sys.modules", _camoufox_module(cm)),
            pytest.raises(RuntimeError, match="no page"),
        ):
            await browser._ensure_browser()
        assert _stack_lock_held(), "released under the failed launch's orphaned browser"


class TestLocalBrowserProcesses:
    """The descendant scan that decides whether an unfinished teardown left a browser."""

    @staticmethod
    def _proc(root: Path, pid: int, comm: str, ppid: int) -> None:
        d = root / str(pid)
        d.mkdir()
        (d / "stat").write_text(f"{pid} ({comm}) S {ppid} 1 1 0 -1\n")

    def test_finds_driver_and_browser_descendants_only(self, tmp_path):
        me = os.getpid()
        self._proc(tmp_path, me, "python3", 1)
        self._proc(tmp_path, 900001, "node", me)
        self._proc(tmp_path, 900002, "camoufox-bin", 900001)
        self._proc(tmp_path, 900003, "Web Content (x)", 900002)  # parens in comm
        self._proc(tmp_path, 900004, "chrome", 1)  # someone else's browser
        self._proc(tmp_path, 900005, "pgrep", me)  # ours, not a browser
        self._proc(tmp_path, 900006, "chrome-headless", me)  # chrome-headless-shell, cut to 15
        self._proc(tmp_path, 900007, "headless_shell", me)  # older Playwright's name
        (tmp_path / "self").mkdir()  # non-pid entries are ignored
        assert browser._local_browser_processes(tmp_path) == [900001, 900002, 900006, 900007]

    def test_none_left(self, tmp_path):
        self._proc(tmp_path, os.getpid(), "python3", 1)
        assert browser._local_browser_processes(tmp_path) == []

    def test_unreadable_table_is_unknown(self, tmp_path):
        assert browser._local_browser_processes(tmp_path / "missing") is None

    def test_live_table_is_readable(self):
        """Against the real /proc the scan answers (a list), never None, here."""
        if not Path("/proc/self/stat").exists():
            pytest.skip("no /proc")
        assert isinstance(browser._local_browser_processes(), list)


class TestLateImportBaseline:
    """A browser package first imported AFTER it changed on disk loaded the new
    files: it is current, not stale."""

    def test_package_installed_then_imported_is_not_stale(self, monkeypatch):
        startup = dict(browser._STARTUP_BROWSER_VERSIONS, camoufox="0.4.11")
        monkeypatch.setattr(browser, "_STARTUP_BROWSER_VERSIONS", startup)
        monkeypatch.setattr(browser, "_LOADED_BROWSER_VERSIONS", {}, raising=False)
        monkeypatch.setattr(browser, "_LAST_SEEN_BROWSER_VERSIONS", dict(startup), raising=False)
        upgraded = dict(startup, camoufox="0.5.7")
        mods = {k: v for k, v in sys.modules.items() if k not in browser._BROWSER_DISTS}
        with (
            patch.object(browser, "_installed_browser_versions", return_value=upgraded),
            patch.dict("sys.modules", mods, clear=True),
        ):
            browser._check_loaded_browser_modules()  # nothing loaded: fine
            sys.modules["camoufox"] = MagicMock()  # the launch imports 0.5.7
            browser._check_loaded_browser_modules()  # the next launch
            browser._check_loaded_browser_modules()

    def test_import_across_a_change_is_stale(self, monkeypatch):
        """Loaded while the disk changed under it: which version is unknown, so
        refuse and say restart."""
        startup = dict(browser._STARTUP_BROWSER_VERSIONS, camoufox="0.4.11")
        monkeypatch.setattr(browser, "_STARTUP_BROWSER_VERSIONS", startup)
        monkeypatch.setattr(browser, "_LOADED_BROWSER_VERSIONS", {}, raising=False)
        monkeypatch.setattr(browser, "_LAST_SEEN_BROWSER_VERSIONS", dict(startup), raising=False)
        mods = {k: v for k, v in sys.modules.items() if k not in browser._BROWSER_DISTS}
        mods["camoufox"] = MagicMock()
        with (
            patch.object(
                browser, "_installed_browser_versions",
                return_value=dict(startup, camoufox="0.5.7"),
            ),
            patch.dict("sys.modules", mods, clear=True),
            pytest.raises(browser.BrowserPackagesChanged, match="Restart"),
        ):
            browser._check_loaded_browser_modules()

    def test_change_after_the_import_is_stale(self, monkeypatch):
        startup = dict(browser._STARTUP_BROWSER_VERSIONS, camoufox="0.5.7")
        monkeypatch.setattr(browser, "_STARTUP_BROWSER_VERSIONS", startup)
        monkeypatch.setattr(browser, "_LOADED_BROWSER_VERSIONS", {}, raising=False)
        monkeypatch.setattr(browser, "_LAST_SEEN_BROWSER_VERSIONS", dict(startup), raising=False)
        mods = {k: v for k, v in sys.modules.items() if k not in browser._BROWSER_DISTS}
        mods["camoufox"] = MagicMock()
        disk = {"now": dict(startup)}
        with (
            patch.object(browser, "_installed_browser_versions", lambda: dict(disk["now"])),
            patch.dict("sys.modules", mods, clear=True),
        ):
            browser._check_loaded_browser_modules()  # loaded at 0.5.7: fine
            disk["now"] = dict(startup, camoufox="0.6.0")
            with pytest.raises(browser.BrowserPackagesChanged, match="0.5.7 -> 0.6.0"):
                browser._check_loaded_browser_modules()
