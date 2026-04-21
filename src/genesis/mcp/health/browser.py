"""Browser automation tools for genesis-health MCP.

Provides lightweight, on-demand browser tools with lazy initialization.
The browser launches only when the first navigation/interaction tool is called,
stays warm for the session, and shuts down when the MCP server exits.

Uses a persistent Chrome profile at ~/.genesis/browser-profile/ so cookies,
localStorage, and login sessions survive across MCP restarts.

Token-efficient: returns accessibility tree snapshots (YAML-like text) instead
of raw DOM or screenshots by default.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from genesis.mcp.health import mcp

logger = logging.getLogger(__name__)

_PROFILE_DIR = Path.home() / ".genesis" / "browser-profile"

# Module-level browser state — persists across tool calls within a session.
_playwright = None
_context = None
_page = None
_stealth_cm = None  # Camoufox context manager (for proper __aexit__)
_stealth_browser = None
_stealth_page = None
_active_page = None  # Tracks whichever page was last navigated (standard or stealth)

# Collaborative mode — when True, browser launches headed on virtual display :99.
# User watches/interacts via noVNC at http://<tailscale-ip>:6080/vnc.html
_collaborate_mode = False

_SCREENSHOT_DIR = Path.home() / "tmp"
_VNC_DISPLAY = ":99"


async def async_cleanup():
    """Shut down browser. Called from MCP lifespan or manually."""
    global _playwright, _context, _page, _stealth_cm, _stealth_browser, _stealth_page, _active_page
    _active_page = None
    if _context is not None:
        try:
            await _context.close()
        except Exception:
            logger.debug("Browser context cleanup failed", exc_info=True)
        _context = None
        _page = None
    if _playwright is not None:
        try:
            await _playwright.stop()
        except Exception:
            logger.debug("Playwright cleanup failed", exc_info=True)
        _playwright = None
    if _stealth_cm is not None:
        try:
            await _stealth_cm.__aexit__(None, None, None)
        except Exception:
            logger.debug("Camoufox cleanup failed", exc_info=True)
        _stealth_cm = None
        _stealth_browser = None
        _stealth_page = None


async def _ensure_browser():
    """Lazily initialize the Playwright browser with persistent profile.

    Returns the active page. Raises ImportError if playwright is not installed.
    In collaborate mode, launches headed on virtual display :99 for VNC sharing.
    """
    global _playwright, _context, _page

    if _page is not None:
        return _page

    from playwright.async_api import async_playwright

    _PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    headed = _collaborate_mode
    if headed:
        os.environ["DISPLAY"] = _VNC_DISPLAY

    _playwright = await async_playwright().start()
    _context = await _playwright.chromium.launch_persistent_context(
        user_data_dir=str(_PROFILE_DIR),
        headless=not headed,
        args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"]
        + (["--start-maximized"] if headed else []),
        viewport={"width": 1920, "height": 1080} if headed else None,
    )
    _page = _context.pages[0] if _context.pages else await _context.new_page()
    mode_str = "headed (collaborate)" if headed else "headless"
    logger.info("Browser launched %s with persistent profile at %s", mode_str, _PROFILE_DIR)
    return _page


async def _ensure_stealth_browser():
    """Lazily initialize Camoufox for anti-detection browsing.

    Returns the active stealth page. Raises ImportError if camoufox is not installed.
    """
    global _stealth_cm, _stealth_browser, _stealth_page

    if _stealth_page is not None:
        return _stealth_page

    from camoufox.async_api import AsyncCamoufox

    _stealth_cm = AsyncCamoufox(headless=True)
    _stealth_browser = await _stealth_cm.__aenter__()
    _stealth_page = await _stealth_browser.new_page()
    logger.info("Camoufox stealth browser launched")
    return _stealth_page


async def _get_page(stealth: bool = False):
    """Get the appropriate browser page based on mode.

    Sets _active_page so subsequent interaction tools (click, fill, etc.)
    use whichever browser was last navigated.
    """
    global _active_page
    _active_page = await _ensure_stealth_browser() if stealth else await _ensure_browser()
    return _active_page


async def _snapshot_page(page) -> str:
    """Get accessibility tree snapshot of the current page."""
    try:
        return await page.locator("body").aria_snapshot()
    except Exception as e:
        return f"(snapshot unavailable: {e})"


# ---------------------------------------------------------------------------
# Tool implementations (testable without FastMCP)
# ---------------------------------------------------------------------------


async def _impl_browser_navigate(url: str, stealth: bool = False) -> dict:
    """Navigate to a URL and return the page snapshot."""
    try:
        page = await _get_page(stealth)
    except ImportError as e:
        return {"error": f"Browser not available: {e}. Install with: pip install playwright"}

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        snapshot = await _snapshot_page(page)
        return {
            "url": page.url,
            "title": await page.title(),
            "snapshot": snapshot,
        }
    except Exception as e:
        logger.error("browser_navigate failed: %s", e, exc_info=True)
        return {"error": str(e), "url": url}


async def _impl_browser_click(selector: str) -> dict:
    """Click an element on the current page."""
    if _active_page is None:
        return {"error": "No page open. Call browser_navigate first."}
    try:
        await _active_page.click(selector, timeout=10000)
        snapshot = await _snapshot_page(_active_page)
        return {"clicked": selector, "url": _active_page.url, "snapshot": snapshot}
    except Exception as e:
        return {"error": f"Click failed on '{selector}': {e}"}


async def _impl_browser_fill(selector: str, value: str) -> dict:
    """Fill a form field on the current page."""
    if _active_page is None:
        return {"error": "No page open. Call browser_navigate first."}
    try:
        await _active_page.fill(selector, value, timeout=10000)
        return {"filled": selector, "url": _active_page.url}
    except Exception as e:
        return {"error": f"Fill failed on '{selector}': {e}"}


async def _impl_browser_screenshot() -> dict:
    """Take a screenshot of the current page."""
    if _active_page is None:
        return {"error": "No page open. Call browser_navigate first."}
    try:
        _SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        screenshot_path = _SCREENSHOT_DIR / "genesis_browser_screenshot.png"
        await _active_page.screenshot(path=str(screenshot_path))
        return {
            "path": str(screenshot_path),
            "url": _active_page.url,
            "title": await _active_page.title(),
        }
    except Exception as e:
        return {"error": f"Screenshot failed: {e}"}


async def _impl_browser_snapshot() -> dict:
    """Return the accessibility tree snapshot of the current page."""
    if _active_page is None:
        return {"error": "No page open. Call browser_navigate first."}
    try:
        snapshot = await _snapshot_page(_active_page)
        return {"url": _active_page.url, "title": await _active_page.title(), "snapshot": snapshot}
    except Exception as e:
        return {"error": f"Snapshot failed: {e}"}


async def _impl_browser_run_js(expression: str) -> dict:
    """Execute JavaScript on the current page and return the result.

    Runs JS in the browser's V8 engine via Playwright page.evaluate().
    Equivalent to Chrome DevTools console. Expressions are logged for audit.
    """
    if _active_page is None:
        return {"error": "No page open. Call browser_navigate first."}
    try:
        logger.info("browser_run_js: %s", expression[:200])
        result = await _active_page.evaluate(expression)
        return {"result": result, "url": _active_page.url}
    except Exception as e:
        return {"error": f"JS execution failed: {e}"}


async def _impl_browser_sessions() -> dict:
    """List logged-in sessions from the persistent browser profile.

    Does NOT launch a browser — reads the cookie database directly.
    """
    try:
        from genesis.browser.profile import BrowserProfileManager
        mgr = BrowserProfileManager()
        info = mgr.get_info()
        return {
            "profile_path": info.profile_path,
            "exists": info.exists,
            "size_mb": info.size_mb,
            "sessions": [
                {"domain": s.domain, "cookie_count": s.cookie_count}
                for s in info.sessions
            ],
        }
    except Exception as e:
        return {"error": f"Failed to read browser sessions: {e}"}


async def _impl_browser_clear_domain(domain: str) -> dict:
    """Clear cookies for a specific domain (selective logout).

    Does NOT launch a browser — modifies the cookie database directly.
    """
    try:
        from genesis.browser.profile import BrowserProfileManager
        mgr = BrowserProfileManager()
        removed = mgr.clear_domain(domain)
        return {"domain": domain, "cookies_removed": removed}
    except Exception as e:
        return {"error": f"Failed to clear domain '{domain}': {e}"}


# ---------------------------------------------------------------------------
# MCP tool registrations
# ---------------------------------------------------------------------------


@mcp.tool()
async def browser_navigate(url: str, stealth: bool = False) -> dict:
    """Navigate to a URL and return an accessibility tree snapshot.

    The browser uses a persistent profile at ~/.genesis/browser-profile/ so
    cookies and login sessions survive across calls.

    Set stealth=True to use Camoufox (anti-detection Firefox) for sites that
    block automated browsers. Stealth mode uses a separate profile.
    """
    return await _impl_browser_navigate(url, stealth)


@mcp.tool()
async def browser_click(selector: str) -> dict:
    """Click an element on the current page by CSS selector or text.

    Examples: '#submit-btn', 'text=Sign In', '[data-testid="login"]'
    Returns the updated page snapshot after clicking.
    """
    return await _impl_browser_click(selector)


@mcp.tool()
async def browser_fill(selector: str, value: str) -> dict:
    """Fill a form field on the current page.

    Examples: browser_fill('#email', 'user@example.com')
    """
    return await _impl_browser_fill(selector, value)


@mcp.tool()
async def browser_screenshot() -> dict:
    """Take a screenshot of the current page.

    Saves to ~/tmp/genesis_browser_screenshot.png and returns the path.
    Use the Read tool to view the image.
    """
    return await _impl_browser_screenshot()


@mcp.tool()
async def browser_snapshot() -> dict:
    """Return the accessibility tree of the current page.

    Token-efficient alternative to screenshots. Returns structured text
    showing all interactive elements, headings, and content.
    """
    return await _impl_browser_snapshot()


@mcp.tool()
async def browser_run_js(expression: str) -> dict:
    """Execute JavaScript in the browser's console on the current page.

    Runs the expression in the page's V8 engine context, equivalent to
    Chrome DevTools console. Returns the expression result.

    Example: browser_run_js('document.title')
    """
    return await _impl_browser_run_js(expression)


@mcp.tool()
async def browser_sessions() -> dict:
    """List logged-in sessions from the persistent browser profile.

    Reads the Chrome cookie database without launching a browser.
    Shows which domains have saved cookies/sessions.
    """
    return await _impl_browser_sessions()


@mcp.tool()
async def browser_clear_domain(domain: str) -> dict:
    """Clear cookies for a specific domain (selective logout).

    Modifies the cookie database directly without launching a browser.
    Example: browser_clear_domain('github.com')
    """
    return await _impl_browser_clear_domain(domain)


@mcp.tool()
async def browser_collaborate(enable: bool = True) -> dict:
    """Toggle collaborative browser mode (headed + VNC).

    When enabled, the browser runs visibly on a virtual display.
    The user can watch and interact via noVNC in their browser.
    Useful for tasks requiring human input (captchas, payments, 2FA).

    When disabled, reverts to headless mode for faster automation.
    Toggling restarts the browser — existing page state is lost.
    """
    global _collaborate_mode

    if _collaborate_mode == enable:
        return {
            "mode": "collaborate" if enable else "headless",
            "changed": False,
            "vnc_url": _get_vnc_url() if enable else None,
        }

    _collaborate_mode = enable
    await async_cleanup()  # Force browser restart on next tool call

    return {
        "mode": "collaborate" if enable else "headless",
        "changed": True,
        "vnc_url": _get_vnc_url() if enable else None,
        "note": "Browser will relaunch in new mode on next navigation.",
    }


def _get_vnc_url() -> str:
    """Derive the noVNC URL from Tailscale IP or fall back to localhost."""
    import subprocess

    try:
        result = subprocess.run(
            ["tailscale", "ip", "-4"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            ip = result.stdout.strip().split("\n")[0]
            return f"http://{ip}:6080/vnc.html"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return "http://localhost:6080/vnc.html"
