"""Browser automation tools for genesis-health MCP.

Provides lightweight, on-demand browser tools with lazy initialization.
The browser launches only when the first navigation/interaction tool is called,
stays warm for the session, and shuts down when the MCP server exits.

Primary browser: Camoufox (anti-detection Firefox). Persistent profile at
~/.genesis/camoufox-profile/ so cookies, localStorage, and login sessions
survive across MCP restarts. Chromium is the fallback for compatibility.

Token-efficient: returns accessibility tree snapshots (YAML-like text) instead
of raw DOM or screenshots by default.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import random
import time
from pathlib import Path

from genesis.mcp.health import mcp

logger = logging.getLogger(__name__)

# Prevents concurrent browser init/cleanup races across tool calls.
_browser_lock = asyncio.Lock()

_PROFILE_DIR = Path.home() / ".genesis" / "camoufox-profile"
_CHROMIUM_PROFILE_DIR = Path.home() / ".genesis" / "browser-profile"

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

# Idle timeout — auto-cleanup browser after 1 hour of no tool calls.
# User-approved value (2026-04-21). Background asyncio task polls every 60s.
_last_used: float = 0.0
_idle_task: asyncio.Task | None = None
_IDLE_TIMEOUT_S = 3600  # 1 hour

_SCREENSHOT_DIR = Path.home() / "tmp"
_VNC_DISPLAY = ":99"


def _is_page_alive(page) -> bool:
    """Check if a Playwright page reference is still usable.

    Synchronous fast-path check.  Catches the most common failure modes
    (closed pages, disposed objects).  Some stale-page scenarios where the
    browser process died but the page object has cached state may slip
    through — those are caught by try/except in the tool implementations.
    """
    try:
        if page.is_closed():
            return False
        _ = page.url  # raises on disposed objects
        return True
    except Exception:
        return False


async def async_cleanup():
    """Shut down browser. Called from MCP lifespan, idle timeout, or manually.

    Safe to call when the browser is already dead — all steps are
    individually guarded so a crashed Camoufox won't hang cleanup.
    Each cleanup step has a 10s timeout (user-approved) to prevent hanging
    if the Playwright Node.js driver or browser process is stuck. Orphaned
    processes that survive timeout are caught by the process reaper (4h cycle).
    """
    global _playwright, _context, _page, _stealth_cm, _stealth_browser, _stealth_page, _active_page
    global _idle_task, _last_used

    _active_page = None

    # Cancel idle watcher first — prevent re-entrant cleanup
    if _idle_task is not None:
        _idle_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _idle_task
        _idle_task = None
    _last_used = 0.0

    if _context is not None:
        try:
            await asyncio.wait_for(_context.close(), timeout=10.0)
        except TimeoutError:
            logger.warning("Browser context close timed out (10s)")
        except Exception:
            logger.debug("Browser context cleanup failed", exc_info=True)
        _context = None
        _page = None
    if _playwright is not None:
        try:
            await asyncio.wait_for(_playwright.stop(), timeout=10.0)
        except TimeoutError:
            logger.warning("Playwright stop timed out (10s) — driver may be orphaned")
        except Exception:
            logger.debug("Playwright cleanup failed", exc_info=True)
        _playwright = None
    if _stealth_cm is not None:
        try:
            await asyncio.wait_for(
                _stealth_cm.__aexit__(None, None, None), timeout=10.0,
            )
        except TimeoutError:
            logger.warning("Camoufox cleanup timed out (10s)")
        except Exception:
            logger.debug("Camoufox cleanup failed", exc_info=True)
        _stealth_cm = None
        _stealth_browser = None
        _stealth_page = None


async def _ensure_browser():
    """Lazily initialize Camoufox (primary browser) with persistent profile.

    Returns the active page. Raises ImportError if camoufox is not installed.
    In collaborate mode, launches headed on virtual display :99 for VNC sharing.
    Uses anti-detection Firefox by default for all browsing.

    Detects stale pages (e.g. browser killed by a concurrent session) and
    automatically cleans up + re-initializes.
    """
    global _stealth_cm, _stealth_browser, _stealth_page

    async with _browser_lock:
        if _stealth_page is not None:
            if _is_page_alive(_stealth_page):
                return _stealth_page
            logger.warning("Camoufox page is stale — restarting browser")
            await async_cleanup()

        from camoufox.async_api import AsyncCamoufox

        _PROFILE_DIR.mkdir(parents=True, exist_ok=True)

        # Always headed — Xvfb :99 is always running.
        os.environ["DISPLAY"] = _VNC_DISPLAY

        _stealth_cm = AsyncCamoufox(
            headless=False,
            persistent_context=True,
            user_data_dir=str(_PROFILE_DIR),
            humanize=2.5,  # Native Camoufox cursor humanization (Bézier curves, max 2.5s)
            firefox_user_prefs={
                # Camoufox disables session history (max_entries=0) for
                # anti-detection.  Re-enable it so back/forward navigation
                # works in collaborate mode.
                "browser.sessionhistory.max_entries": 10,
                "browser.sessionhistory.max_total_viewers": -1,
            },
        )
        _stealth_browser = await _stealth_cm.__aenter__()
        # With persistent_context, browser IS the context
        _stealth_page = _stealth_browser.pages[0] if _stealth_browser.pages else await _stealth_browser.new_page()
        mode_str = "headed (collaborate)" if _collaborate_mode else "headed"
        logger.info("Camoufox browser launched %s with persistent profile at %s", mode_str, _PROFILE_DIR)
        return _stealth_page


async def _ensure_chromium_fallback():
    """Lazily initialize Playwright Chromium as fallback browser.

    Use only when Camoufox fails on a specific site. Persistent profile at
    ~/.genesis/browser-profile/ (separate from Camoufox profile).

    Detects stale pages and automatically re-initializes.
    """
    global _playwright, _context, _page

    async with _browser_lock:
        if _page is not None:
            if _is_page_alive(_page):
                return _page
            logger.warning("Chromium page is stale — restarting browser")
            await async_cleanup()

        from playwright.async_api import async_playwright

        _CHROMIUM_PROFILE_DIR.mkdir(parents=True, exist_ok=True)

        # Always headed — Xvfb :99 is always running.
        os.environ["DISPLAY"] = _VNC_DISPLAY

        _playwright = await async_playwright().start()
        _context = await _playwright.chromium.launch_persistent_context(
            user_data_dir=str(_CHROMIUM_PROFILE_DIR),
            headless=False,
            args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
                  "--start-maximized"],
            viewport={"width": 1280, "height": 720},
        )
        _page = _context.pages[0] if _context.pages else await _context.new_page()
        mode_str = "headed (collaborate)" if _collaborate_mode else "headed"
        logger.info("Chromium fallback launched %s with profile at %s", mode_str, _CHROMIUM_PROFILE_DIR)
        return _page


def _touch():
    """Record browser activity timestamp for idle timeout tracking."""
    global _last_used
    _last_used = time.monotonic()


async def _idle_watcher_loop():
    """Background task: cleanup browser after idle timeout (1 hour).

    Polls every 60s. When the browser has been idle for _IDLE_TIMEOUT_S,
    calls async_cleanup() and exits. CancelledError is the normal shutdown
    path (MCP lifespan exit or explicit cleanup).
    """
    try:
        while True:
            await asyncio.sleep(60)
            if _last_used > 0 and (time.monotonic() - _last_used) >= _IDLE_TIMEOUT_S:
                logger.info("Browser idle for %ds — auto-cleaning up", _IDLE_TIMEOUT_S)
                await async_cleanup()
                return
    except asyncio.CancelledError:
        return


def _start_idle_watcher():
    """Start the idle watcher task if not already running."""
    global _idle_task
    if _idle_task is None or _idle_task.done():
        from genesis.util.tasks import tracked_task

        _idle_task = tracked_task(
            _idle_watcher_loop(), name="browser-idle-watcher",
        )


async def _get_page(stealth: bool = False):
    """Get the appropriate browser page based on mode.

    Default (stealth=False): Camoufox (anti-detection, primary).
    Fallback (stealth=True): Chromium (for Camoufox-incompatible sites).
    Note: 'stealth' param is inverted from its old meaning for API compat.

    Sets _active_page so subsequent interaction tools (click, fill, etc.)
    use whichever browser was last navigated.
    """
    global _active_page
    _active_page = await _ensure_chromium_fallback() if stealth else await _ensure_browser()
    _touch()
    _start_idle_watcher()
    return _active_page


async def _snapshot_page(page) -> str:
    """Get accessibility tree snapshot of the current page."""
    try:
        return await page.locator("body").aria_snapshot()
    except Exception as e:
        return f"(snapshot unavailable: {e})"


# ---------------------------------------------------------------------------
# Human-like interaction timing
# ---------------------------------------------------------------------------


def _is_camoufox_active() -> bool:
    """True when the active browser is Camoufox (anti-detection mode)."""
    return _stealth_cm is not None and _active_page is _stealth_page


async def _human_delay() -> None:
    """Random delay mimicking human interaction timing.

    Only fires when Camoufox (anti-detection browser) is active.
    Playwright/Chromium mode skips delays entirely — that's for dev/test.

    Background (default): 1.0–15.0s, log-normal distribution.
    Stealth priority — nobody watching, look maximally human.

    Collaborate mode (VNC): 0.5–2.0s, uniform.
    Human watching — keep it responsive but not instant.
    """
    if not _is_camoufox_active():
        return
    if _collaborate_mode:
        await asyncio.sleep(random.uniform(0.5, 2.0))
    else:
        # Log-normal: mostly 2-5s with occasional longer pauses up to ~15s
        delay = min(random.lognormvariate(1.2, 0.6), 15.0)
        delay = max(delay, 1.0)
        await asyncio.sleep(delay)


# ---------------------------------------------------------------------------
async def _stealth_click(page, selector: str, timeout: int = 10000) -> None:
    """Human-like click: hover first, jitter position, realistic event chain.

    When Camoufox is active, generates a mousemove trail to the element
    before clicking with a slight offset from center.  This produces
    mousemove → mouseenter → mousedown → mouseup → click event chains
    that match real human behavior.

    Falls back to plain page.click() when not in stealth mode.
    """
    if not _is_camoufox_active():
        await page.click(selector, timeout=timeout)
        return

    try:
        el = await page.wait_for_selector(selector, timeout=timeout)
        if el is None:
            raise Exception(f"Element not found: {selector}")
        box = await el.bounding_box()
        if box is None:
            await page.click(selector, timeout=timeout)
            return

        # Jitter: click within central 60% of element, not dead center
        jitter_x = random.uniform(box["width"] * 0.2, box["width"] * 0.8)
        jitter_y = random.uniform(box["height"] * 0.2, box["height"] * 0.8)
        target_x = box["x"] + jitter_x
        target_y = box["y"] + jitter_y

        # Hover first — generates mousemove trail to the element
        await page.mouse.move(target_x, target_y, steps=random.randint(5, 15))
        await asyncio.sleep(random.uniform(0.05, 0.2))

        # Click with realistic mousedown/mouseup gap
        await page.mouse.down()
        await asyncio.sleep(random.uniform(0.04, 0.12))
        await page.mouse.up()
    except Exception:
        logger.warning("Stealth click fallback for '%s'", selector)
        await page.click(selector, timeout=timeout)


async def _human_type(page, selector: str, value: str) -> None:
    """Type text character-by-character with human-like timing.

    Camoufox mode: clears field via fill(""), then types per-keystroke
    with randomized inter-key intervals.  This fires the full
    keydown → keypress/input → keyup event chain per character that
    behavioral detection systems expect from real users.

    Uses per-character randomization via keyboard.type() for true IKI
    jitter (Playwright's page.type delay= is fixed across all chars).

    Non-Camoufox: falls back to atomic page.fill() (no delay overhead).
    """
    if not _is_camoufox_active():
        await page.fill(selector, value, timeout=10000)
        return

    # Clear field reliably (works on React controlled inputs)
    await page.fill(selector, "", timeout=10000)
    # Click to focus the field
    await page.click(selector, timeout=10000)
    # Type per-keystroke with TRUE per-character IKI jitter
    for char in value:
        await page.keyboard.type(char)
        iki = random.uniform(0.05, 0.20)  # 50-200ms
        # 5% chance of a "thinking pause" (300-1000ms)
        if random.random() < 0.05:
            iki = random.uniform(0.3, 1.0)
        await asyncio.sleep(iki)


async def _send_turnstile_alert(page_url: str) -> None:
    """Send Telegram alert that a CAPTCHA needs human intervention.

    Uses TelegramAlertChannel (stdlib urllib — no external deps).
    Reads credentials from secrets.env.  Never raises — alert failure
    must not crash the browser submission.
    """
    try:
        from genesis.guardian.alert.base import Alert, AlertSeverity
        from genesis.guardian.alert.telegram import TelegramAlertChannel

        secrets_path = Path.home() / "genesis" / "secrets.env"
        if not secrets_path.exists():
            logger.warning("secrets.env not found — cannot send CAPTCHA alert")
            return

        secrets: dict[str, str] = {}
        for line in secrets_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                secrets[k.strip()] = v.strip().strip("'\"")

        bot_token = secrets.get("TELEGRAM_BOT_TOKEN", "")
        chat_id = secrets.get("TELEGRAM_FORUM_CHAT_ID") or secrets.get("TELEGRAM_CHAT_ID", "")
        if not bot_token or not chat_id:
            logger.warning("Telegram credentials missing — cannot send CAPTCHA alert")
            return

        vnc_url = _get_vnc_url()
        channel = TelegramAlertChannel(bot_token, chat_id)
        alert = Alert(
            severity=AlertSeverity.WARNING,
            title="CAPTCHA Challenge Detected",
            body=(
                f"Browser at {page_url} hit a Cloudflare Turnstile challenge "
                f"that won't auto-resolve.\n\n"
                f"Open VNC to solve it: {vnc_url}"
            ),
        )
        await channel.send(alert)
        logger.info("CAPTCHA alert sent to Telegram for %s", page_url)
    except Exception:
        logger.warning("Failed to send CAPTCHA Telegram alert", exc_info=True)


async def _poll_turnstile_token(page, timeout_s: float, interval_s: float) -> bool:
    """Poll for Cloudflare Turnstile response token.  Returns True if found."""
    start = asyncio.get_running_loop().time()
    while (asyncio.get_running_loop().time() - start) < timeout_s:
        token = await page.evaluate("""() => {
            const inp = document.querySelector(
                'input[name="cf-turnstile-response"]'
            );
            return inp ? inp.value : '';
        }""")
        if token:
            return True
        await asyncio.sleep(interval_s)
    return False


async def _wait_for_turnstile(page, timeout_ms: int = 15000) -> dict | None:
    """Detect and handle Cloudflare Turnstile challenge.

    Phase 1 (auto-resolve): Polls for up to ``timeout_ms`` for the Turnstile
    iframe to produce a ``cf-turnstile-response`` token.  Many challenges
    auto-resolve for legitimate-looking browsers.

    Phase 2 (human escalation): If auto-resolve fails, sends a Telegram
    alert and polls for up to 5 minutes for human intervention via VNC.
    The browser is always headed on :99, so the human can see and interact
    with it immediately — no restart needed.

    Returns None if no Turnstile detected, or a status dict.
    """
    try:
        # Brief delay for SPA-injected Turnstile iframes
        await asyncio.sleep(0.8)

        turnstile = await page.query_selector(
            'iframe[src*="challenges.cloudflare.com"]'
        )
        if turnstile is None:
            return None

        logger.info("Turnstile challenge detected — waiting for auto-resolve")

        # Phase 1: auto-resolve
        if await _poll_turnstile_token(page, timeout_ms / 1000, 1.0):
            logger.info("Turnstile auto-resolved")
            await asyncio.sleep(random.uniform(1.0, 3.0))
            return {"status": "resolved", "method": "auto"}

        # Phase 2: escalate to human
        logger.warning("Turnstile did NOT auto-resolve — escalating to human via Telegram")
        await _send_turnstile_alert(page.url)

        if await _poll_turnstile_token(page, 300, 5.0):  # 5 minutes
            logger.info("Turnstile resolved by human intervention")
            await asyncio.sleep(random.uniform(1.0, 2.0))
            return {"status": "resolved", "method": "human"}

        logger.warning("Turnstile NOT resolved after 5 min escalation")
        return {"status": "blocked", "method": "timeout"}
    except Exception as e:
        logger.debug("Turnstile detection error: %s", e)
        return None


# Tool implementations (testable without FastMCP)
# ---------------------------------------------------------------------------


async def _impl_browser_navigate(url: str, stealth: bool = False) -> dict:
    """Navigate to a URL and return the page snapshot."""
    _touch()
    try:
        page = await _get_page(stealth)
    except ImportError as e:
        return {"error": f"Browser not available: {e}. Install with: pip install playwright"}

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)

        # Detect and handle Cloudflare Turnstile if present
        turnstile_result = None
        if _is_camoufox_active():
            turnstile_result = await _wait_for_turnstile(page)

        snapshot = await _snapshot_page(page)
        result = {
            "url": page.url,
            "title": await page.title(),
            "snapshot": snapshot,
        }
        if turnstile_result:
            result["turnstile"] = turnstile_result
            if turnstile_result["status"] == "blocked":
                result["warning"] = (
                    "Cloudflare Turnstile challenge did not resolve. "
                    "A Telegram alert was sent. Check VNC if you can still assist."
                )
        return result
    except Exception as e:
        logger.error("browser_navigate failed: %s", e, exc_info=True)
        return {"error": str(e), "url": url}


async def _impl_browser_click(selector: str) -> dict:
    """Click an element on the current page."""
    _touch()
    if _active_page is None:
        return {"error": "No page open. Call browser_navigate first."}
    try:
        await _human_delay()
        await _stealth_click(_active_page, selector)
        snapshot = await _snapshot_page(_active_page)
        return {"clicked": selector, "url": _active_page.url, "snapshot": snapshot}
    except Exception as e:
        return {"error": f"Click failed on '{selector}': {e}"}


async def _impl_browser_fill(selector: str, value: str) -> dict:
    """Fill a form field on the current page."""
    _touch()
    if _active_page is None:
        return {"error": "No page open. Call browser_navigate first."}
    try:
        await _human_delay()
        await _human_type(_active_page, selector, value)
        return {"filled": selector, "url": _active_page.url}
    except Exception as e:
        return {"error": f"Fill failed on '{selector}': {e}"}


async def _impl_browser_upload(selector: str, file_path: str) -> dict:
    """Upload a file to a file input element on the current page."""
    if _active_page is None:
        return {"error": "No page open. Call browser_navigate first."}
    p = Path(file_path)
    if not p.is_file():
        return {"error": f"File not found or not a regular file: {file_path}"}
    try:
        await _human_delay()
        await _active_page.set_input_files(selector, str(p), timeout=10000)
        return {"uploaded": p.name, "selector": selector, "url": _active_page.url}
    except Exception as e:
        return {"error": f"Upload failed on '{selector}': {e}"}


async def _impl_browser_screenshot() -> dict:
    """Take a screenshot of the current page."""
    _touch()
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
    _touch()
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
    _touch()
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

    Uses Camoufox (anti-detection Firefox) by default with a persistent profile
    at ~/.genesis/camoufox-profile/ so cookies and logins survive across calls.

    IMPORTANT: When using Camoufox for stealth browsing (the default), load the
    stealth-browser skill for anti-detection behavioral rules. The skill covers
    timing, interaction patterns, honeypot avoidance, and per-site guidance.

    Set stealth=True to use Chromium fallback for sites incompatible with
    Camoufox (rare). Chromium uses a separate profile at ~/.genesis/browser-profile/.

    NOTE: If Cloudflare Turnstile is detected, this call may block for up to
    ~5 minutes while waiting for human resolution via VNC. A Telegram alert
    is sent automatically. The response will include a 'turnstile' field with
    the resolution status.
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
async def browser_upload(selector: str, file_path: str) -> dict:
    """Upload a file to a file input element on the current page.

    Use for <input type="file"> elements (resume uploads, document attachments).
    The file must exist at the given path.

    Examples: browser_upload('input[type=file]', '/path/to/resume.pdf')
    """
    return await _impl_browser_upload(selector, file_path)


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
    """Toggle collaborative timing mode.

    The browser always runs headed on VNC display :99 — it's always observable.
    This tool controls the TIMING profile, not visibility:

    - enable=True (collaborate): faster timing (0.5-2s between actions).
      Use when a human is actively watching via VNC.
    - enable=False (background): stealth timing (1-15s between actions).
      Use when nobody is watching — maximally human-like pace.

    No browser restart. No page state loss. Just a timing change.
    """
    global _collaborate_mode

    _collaborate_mode = enable

    vnc_url = _get_vnc_url()
    return {
        "mode": "collaborate" if enable else "background",
        "timing": "fast (0.5-2s)" if enable else "stealth (1-15s)",
        "vnc_url": vnc_url,
        "note": "Browser is always headed on VNC. Open the URL above to watch/interact.",
    }


def _get_vnc_url() -> str:
    """Derive the noVNC URL from Tailscale IP or fall back to localhost."""
    import subprocess

    try:
        result = subprocess.run(
            ["tailscale", "ip", "-4"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if result.returncode == 0 and result.stdout.strip():
            ip = result.stdout.strip().split("\n")[0]
            return f"http://{ip}:6080/vnc_scaled.html"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return "http://localhost:6080/vnc_scaled.html"
