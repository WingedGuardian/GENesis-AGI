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

# Layer 3: CDP remote browser (user's real Chrome over Tailscale)
_remote_pw = None  # Separate Playwright instance (independent lifecycle from _playwright)
_remote_browser = None  # CDP Browser connection
_remote_page = None  # Active page on user's remote Chrome
_remote_cdp_url: str | None = None  # e.g. "http://100.x.y.z:9222"
_remote_last_url: str | None = None  # URL at last Genesis action (drift detection)

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

    # Remote CDP: disconnect (does NOT close user's Chrome)
    await _cleanup_remote_cdp()

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
            window=(1920, 1080),  # Fill VNC display (Xvfb :99 is 1920x1080x24)
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


def _on_remote_disconnected() -> None:
    """Callback when CDP connection drops (Chrome closed, machine asleep)."""
    global _remote_browser, _remote_page, _active_page
    logger.warning("Remote CDP disconnected (Chrome closed or network lost)")
    _remote_browser = None
    if _active_page is _remote_page:
        _active_page = None
    _remote_page = None
    # Preserve _remote_cdp_url so reconnection works on next call


async def _cleanup_remote_cdp() -> None:
    """Disconnect from remote Chrome. Does NOT close the user's browser.

    Playwright's browser.close() on a CDP connection is a disconnect only —
    it does NOT terminate the remote Chrome process.
    """
    global _remote_pw, _remote_browser, _remote_page, _remote_last_url

    if _remote_browser is not None:
        try:
            await asyncio.wait_for(_remote_browser.close(), timeout=10.0)
        except TimeoutError:
            logger.warning("Remote CDP disconnect timed out (10s)")
        except Exception:
            logger.debug("Remote CDP cleanup failed", exc_info=True)
        _remote_browser = None
        _remote_page = None

    if _remote_pw is not None:
        try:
            await asyncio.wait_for(_remote_pw.stop(), timeout=10.0)
        except TimeoutError:
            logger.warning("Remote Playwright stop timed out (10s)")
        except Exception:
            logger.debug("Remote Playwright cleanup failed", exc_info=True)
        _remote_pw = None

    _remote_last_url = None


async def _ensure_remote_cdp(cdp_url: str | None = None):
    """Connect to the user's Chrome via CDP over Tailscale.

    Returns the active remote page. The user must have Chrome running with
    ``--remote-debugging-port=9222``. Connection is via Tailscale IP.

    Does NOT launch Chrome. Does NOT close the user's existing tabs.
    On disconnect, clears state — next call gets a clear error.
    """
    global _remote_pw, _remote_browser, _remote_page, _remote_cdp_url

    async with _browser_lock:
        # Already connected and alive — reuse
        if _remote_page is not None and _remote_browser is not None:
            if _remote_browser.is_connected() and _is_page_alive(_remote_page):
                return _remote_page
            logger.warning("Remote CDP connection stale — cleaning up")
            await _cleanup_remote_cdp()

        # Resolve CDP URL: explicit > env > stored
        url = cdp_url or os.environ.get("GENESIS_CDP_URL") or _remote_cdp_url
        if not url:
            raise ConnectionError(
                "No CDP URL configured. Pass cdp_url parameter or set "
                "GENESIS_CDP_URL in secrets.env.\n\n"
                "User setup: Launch Chrome on your machine with:\n"
                "  chrome.exe --remote-debugging-port=9222 "
                "--user-data-dir=%USERPROFILE%\\chrome-genesis"
            )

        from playwright.async_api import async_playwright

        _remote_pw = await async_playwright().start()
        try:
            _remote_browser = await _remote_pw.chromium.connect_over_cdp(url)
        except Exception as e:
            await _remote_pw.stop()
            _remote_pw = None
            raise ConnectionError(
                f"Cannot connect to Chrome at {url}. Error: {e}\n\n"
                "Check:\n"
                "  1. Chrome is running with --remote-debugging-port=9222\n"
                "  2. Tailscale is connected on both machines\n"
                "  3. Windows firewall allows port 9222 from Tailscale"
            ) from e

        _remote_cdp_url = url
        _remote_browser.on("disconnected", lambda: _on_remote_disconnected())

        # Use existing tab or create new one — never close user's tabs
        contexts = _remote_browser.contexts
        if contexts and contexts[0].pages:
            _remote_page = contexts[0].pages[-1]
            logger.info("CDP remote connected — using existing tab: %s", _remote_page.url)
        elif contexts:
            _remote_page = await contexts[0].new_page()
            logger.info("CDP remote connected — created new tab")
        else:
            ctx = await _remote_browser.new_context()
            _remote_page = await ctx.new_page()
            logger.info("CDP remote connected — created new context and tab")

        return _remote_page


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


async def _get_page(
    stealth: bool = True,
    remote: bool = False,
    cdp_url: str | None = None,
):
    """Get the appropriate browser page based on mode.

    Default (stealth=True): Camoufox (anti-detection, primary).
    Plain (stealth=False): Chromium fallback for Camoufox-incompatible sites.
    Remote (remote=True): User's real Chrome via CDP over Tailscale.

    Sets _active_page so subsequent interaction tools (click, fill, etc.)
    use whichever browser was last navigated.
    """
    global _active_page
    if remote:
        _active_page = await _ensure_remote_cdp(cdp_url)
    elif stealth:
        _active_page = await _ensure_browser()
    else:
        _active_page = await _ensure_chromium_fallback()
    _touch()
    _start_idle_watcher()
    return _active_page


async def _snapshot_page(page) -> str:
    """Get accessibility tree snapshot of the current page."""
    try:
        return await asyncio.wait_for(
            page.locator("body").aria_snapshot(), timeout=15.0
        )
    except TimeoutError:
        logger.warning("Snapshot timed out (15s) — page accessibility tree stuck")
        return "(snapshot timed out after 15s)"
    except Exception as e:
        return f"(snapshot unavailable: {e})"


# ---------------------------------------------------------------------------
# Human-like interaction timing
# ---------------------------------------------------------------------------


def _is_camoufox_active() -> bool:
    """True when the active browser is Camoufox (anti-detection mode)."""
    return _stealth_cm is not None and _active_page is _stealth_page


def _is_remote_active() -> bool:
    """True when the active browser is the remote CDP connection."""
    return _remote_page is not None and _active_page is _remote_page


def _remote_browser_connected() -> bool:
    """Quick check if CDP remote is still connected."""
    return _remote_browser is not None and _remote_browser.is_connected()


def _check_remote_health() -> dict | None:
    """Returns error dict if remote is active but disconnected. None if OK."""
    if not _is_remote_active():
        return None
    if not _remote_browser_connected():
        global _active_page, _remote_page
        _active_page = None
        _remote_page = None
        return {
            "error": (
                "Remote Chrome connection lost. "
                "Ask the user to restart Chrome with --remote-debugging-port=9222, "
                "then call browser_navigate(url, remote=True) to reconnect."
            )
        }
    return None


def _update_remote_url() -> None:
    """Update drift tracking URL after a successful action that may navigate."""
    global _remote_last_url
    if _is_remote_active() and _active_page is not None:
        with contextlib.suppress(Exception):
            _remote_last_url = _active_page.url


def _check_page_drift(page) -> dict | None:
    """Check if the remote page URL changed since Genesis last touched it.

    Returns None if no drift, or a dict describing the change.
    Non-async — uses only the synchronous page.url property.
    """
    if _remote_last_url is None:
        return None
    try:
        current_url = page.url
    except Exception:
        return {
            "drift": "page_inaccessible",
            "detail": "Cannot read page URL — tab may have been closed",
        }
    if current_url != _remote_last_url:
        return {
            "drift": "url_changed",
            "expected": _remote_last_url,
            "actual": current_url,
            "detail": (
                f"Page URL changed from {_remote_last_url} to {current_url} "
                "since last Genesis action"
            ),
        }
    return None


async def _human_delay() -> None:
    """Random delay mimicking human interaction timing.

    Remote CDP: always collaborate timing (user watching their own screen).
    Camoufox background: 1.0–15.0s, log-normal distribution.
    Camoufox collaborate (VNC): 0.5–2.0s, uniform.
    Chromium: no delay (dev/test).
    """
    if _is_remote_active():
        # Remote CDP: user is literally watching their own screen
        await asyncio.sleep(random.uniform(0.5, 2.0))
        return
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
# Tool-level timeout (user-approved: career-ops handoff 2026-04-22)
# ---------------------------------------------------------------------------
# Playwright's internal timeout= parameter does NOT reliably fire with
# Camoufox (patched Firefox).  Confirmed: a page.click(timeout=10000) hung
# for 22 minutes until the browser was killed externally.  This asyncio-level
# wrapper is the ONLY reliable timeout for Camoufox browser tools.
_TOOL_TIMEOUT_S: float = 60.0


async def _with_tool_timeout(
    coro, timeout_s: float = _TOOL_TIMEOUT_S, operation: str = "browser"
) -> dict:
    """Wrap a browser tool coroutine with a hard asyncio timeout.

    Returns a structured ``{"error": ...}`` dict on timeout instead of
    raising, so the MCP caller gets a clean error response.

    On timeout, resets ``_active_page`` to None so subsequent tool calls
    don't operate on a page left in an indeterminate state.
    """
    try:
        return await asyncio.wait_for(coro, timeout=timeout_s)
    except TimeoutError:
        global _active_page
        logger.warning("%s timed out after %.0fs — resetting active page", operation, timeout_s)
        _active_page = None
        return {
            "error": (
                f"{operation} timed out after {timeout_s:.0f}s. "
                "Browser state was reset — call browser_navigate to resume."
            )
        }


# ---------------------------------------------------------------------------
async def _stealth_click(page, selector: str, timeout: int = 10000) -> None:
    """Human-like click: hover first, jitter position, realistic event chain.

    When Camoufox is active, generates a mousemove trail to the element
    before clicking with a slight offset from center.  This produces
    mousemove → mouseenter → mousedown → mouseup → click event chains
    that match real human behavior.

    Falls back to plain page.click() when not in stealth mode.
    """
    # --- Ambiguous text= selector guard ---
    # Bare text= selectors silently match the first element even when
    # multiple exist (e.g., "text=No" on a form with several Yes/No radio
    # groups).  Fail fast with a descriptive error so the caller can use a
    # more specific selector.
    if selector.startswith("text="):
        try:
            count = await asyncio.wait_for(
                page.locator(selector).count(), timeout=5.0
            )
            if count > 1:
                summaries: list[str] = []
                for i in range(min(count, 5)):
                    nth = page.locator(selector).nth(i)
                    tag = await nth.evaluate("e => e.tagName.toLowerCase()")
                    name = await nth.evaluate(
                        "e => (e.getAttribute('name') || e.parentElement?.getAttribute('name') || '')"
                    )
                    summaries.append(f"  {i + 1}. <{tag}> name='{name}'")
                raise Exception(
                    f"Ambiguous selector '{selector}' matches {count} elements:\n"
                    + "\n".join(summaries)
                    + "\nUse a more specific selector: CSS, [name=...][value=...], or role."
                )
        except TimeoutError:
            logger.warning("Ambiguity check timed out for '%s', proceeding", selector)
        except Exception as amb_err:
            if "Ambiguous selector" in str(amb_err):
                raise  # re-raise our own ambiguity error
            logger.warning("Ambiguity check failed for '%s': %s", selector, amb_err)

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
    except Exception as stealth_err:
        logger.warning("Stealth click failed for '%s': %s", selector, stealth_err)
        try:
            await page.click(selector, timeout=timeout)
        except Exception as plain_err:
            logger.warning("Plain click also failed for '%s': %s", selector, plain_err)
            # --- Keyboard fallback (last resort) ---
            # Focus the element and press Space/Enter.  Works for radios,
            # checkboxes, buttons — anything keyboard-navigable per WCAG.
            try:
                el = await page.wait_for_selector(selector, timeout=5000)
                if el:
                    await el.focus()
                    tag = await el.evaluate("e => e.tagName.toLowerCase()")
                    input_type = await el.evaluate(
                        "e => (e.getAttribute('type') || '').toLowerCase()"
                    )
                    if tag == "input" and input_type in ("radio", "checkbox"):
                        await page.keyboard.press("Space")
                    else:
                        await page.keyboard.press("Enter")
                    logger.info("Keyboard fallback succeeded for '%s'", selector)
                    return
            except Exception as kb_err:
                logger.warning("Keyboard fallback also failed for '%s': %s", selector, kb_err)
            # --- Shadow DOM fallback ---
            # Element may be inside an open shadow root that Playwright's
            # selector engine can't pierce (common with Lit/Reddit-style
            # web components).  Walk all shadow roots via JS and click the
            # first match.  Only fires when ALL other strategies failed.
            if await _click_in_shadow_dom(page, selector):
                logger.info("Shadow DOM fallback succeeded for '%s'", selector)
                return
            raise plain_err


async def _click_in_shadow_dom(page, selector: str) -> bool:
    """Walk open shadow roots via JS and click the first matching element.

    Returns True if an element was found and clicked, False otherwise.
    Only handles ``text=`` selectors (by text content) and bare CSS
    selectors (via ``querySelector``).  Closed shadow roots are
    inaccessible from JS — this only covers open shadow DOM.
    """
    is_text = selector.startswith("text=")
    search_value = selector[len("text="):] if is_text else selector

    js = """
    ([searchValue, isText]) => {
        function walk(root) {
            if (isText) {
                const candidates = root.querySelectorAll(
                    'button, a, [role="button"], input[type="submit"], '
                    + 'input[type="button"], [tabindex]'
                );
                for (const el of candidates) {
                    const txt = (el.textContent || el.value || '').trim();
                    if (txt === searchValue) return el;
                }
            } else {
                try {
                    const el = root.querySelector(searchValue);
                    if (el) return el;
                } catch (_) { /* invalid selector — skip */ }
            }
            for (const child of root.querySelectorAll('*')) {
                if (child.shadowRoot) {
                    const found = walk(child.shadowRoot);
                    if (found) return found;
                }
            }
            return null;
        }
        const el = walk(document);
        if (!el) return false;
        el.scrollIntoView({block: 'center'});
        el.click();
        return true;
    }
    """
    try:
        return await page.evaluate(js, [search_value, is_text])
    except Exception as exc:
        logger.debug("Shadow DOM traversal failed for '%s': %s", selector, exc)
        return False


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
        from genesis.env import secrets_path as _secrets_path
        from genesis.guardian.alert.base import Alert, AlertSeverity
        from genesis.guardian.alert.telegram import TelegramAlertChannel

        sec_path = _secrets_path()
        if not sec_path.exists():
            logger.warning("secrets.env not found — cannot send CAPTCHA alert")
            return

        secrets: dict[str, str] = {}
        for line in sec_path.read_text(encoding="utf-8").splitlines():
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


async def _impl_browser_navigate(
    url: str,
    stealth: bool = True,
    remote: bool = False,
    cdp_url: str | None = None,
) -> dict:
    """Navigate to a URL and return the page snapshot."""
    global _collaborate_mode, _remote_last_url
    _touch()

    # Auto-enable collaborate timing for remote CDP (user watching their screen)
    if remote and not _collaborate_mode:
        _collaborate_mode = True
        logger.info("Auto-enabled collaborate timing for remote CDP session")

    try:
        page = await _get_page(stealth, remote=remote, cdp_url=cdp_url)
    except ConnectionError as e:
        return {"error": str(e)}
    except ImportError as e:
        return {"error": f"Browser not available: {e}. Install with: pip install playwright"}

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)

        # Turnstile detection only for Camoufox (real Chrome won't trigger it)
        turnstile_result = None
        if _is_camoufox_active():
            turnstile_result = await _wait_for_turnstile(page)

        # Track URL for drift detection on remote sessions
        if remote:
            _remote_last_url = page.url

        snapshot = await _snapshot_page(page)
        result = {
            "url": page.url,
            "title": await page.title(),
            "snapshot": snapshot,
            "layer": "remote_cdp" if _is_remote_active() else (
                "camoufox" if _is_camoufox_active() else "chromium"
            ),
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
        if remote and not _remote_browser_connected():
            return {
                "error": (
                    "Remote Chrome disconnected during navigation. "
                    "The user may have closed Chrome or the machine went to sleep. "
                    "Ask the user to restart Chrome with --remote-debugging-port=9222, "
                    "then retry."
                ),
                "url": url,
            }
        logger.error("browser_navigate failed: %s", e, exc_info=True)
        return {"error": str(e), "url": url}


async def _impl_browser_click(selector: str) -> dict:
    """Click an element on the current page."""
    _touch()
    if _active_page is None:
        return {"error": "No page open. Call browser_navigate first."}
    health = _check_remote_health()
    if health:
        return health
    drift = _check_page_drift(_active_page) if _is_remote_active() else None
    if drift:
        return {
            "advisory": "Page state changed since last Genesis action.",
            **drift,
            "recommendation": "Call browser_snapshot() to see current page state before acting.",
        }
    try:
        await _human_delay()
        await _stealth_click(_active_page, selector)
        _update_remote_url()  # Click may cause navigation (form submit, link)
        snapshot = await _snapshot_page(_active_page)
        return {"clicked": selector, "url": _active_page.url, "snapshot": snapshot}
    except Exception as e:
        return {"error": f"Click failed on '{selector}': {e}"}


async def _impl_browser_fill(selector: str, value: str) -> dict:
    """Fill a form field on the current page."""
    _touch()
    if _active_page is None:
        return {"error": "No page open. Call browser_navigate first."}
    health = _check_remote_health()
    if health:
        return health
    drift = _check_page_drift(_active_page) if _is_remote_active() else None
    if drift:
        return {
            "advisory": "Page state changed since last Genesis action.",
            **drift,
            "recommendation": "Call browser_snapshot() to see current page state before acting.",
        }
    try:
        await _human_delay()
        await _human_type(_active_page, selector, value)
        _update_remote_url()  # Fill + Enter may cause navigation
        return {"filled": selector, "url": _active_page.url}
    except Exception as e:
        return {"error": f"Fill failed on '{selector}': {e}"}


async def _impl_browser_upload(selector: str, file_path: str) -> dict:
    """Upload a file to a file input element on the current page.

    For remote CDP: file must exist on the Genesis container (Playwright sends
    the file contents over the wire to the remote browser).
    """
    _touch()
    if _active_page is None:
        return {"error": "No page open. Call browser_navigate first."}
    health = _check_remote_health()
    if health:
        return health
    drift = _check_page_drift(_active_page) if _is_remote_active() else None
    if drift:
        return {
            "advisory": "Page state changed since last Genesis action.",
            **drift,
            "recommendation": "Call browser_snapshot() to see current page state before acting.",
        }
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
    health = _check_remote_health()
    if health:
        return health
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
    health = _check_remote_health()
    if health:
        return health
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
    health = _check_remote_health()
    if health:
        return health
    try:
        logger.info("browser_run_js: %s", expression[:200])
        result = await _active_page.evaluate(expression)
        _update_remote_url()  # JS may cause navigation
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


async def _impl_browser_press_key(key: str, count: int = 1) -> dict:
    """Press a keyboard key on the current page."""
    _touch()
    if _active_page is None:
        return {"error": "No page open. Call browser_navigate first."}
    health = _check_remote_health()
    if health:
        return health
    count = max(1, min(count, 50))
    try:
        for i in range(count):
            if i > 0:
                await asyncio.sleep(random.uniform(0.05, 0.15))
            await _active_page.keyboard.press(key)
        return {"pressed": key, "count": count, "url": _active_page.url}
    except Exception as e:
        return {"error": f"Key press failed for '{key}': {e}"}


# ---------------------------------------------------------------------------
# MCP tool registrations
# ---------------------------------------------------------------------------


@mcp.tool()
async def browser_navigate(
    url: str,
    stealth: bool = True,
    remote: bool = False,
    cdp_url: str | None = None,
) -> dict:
    """Navigate to a URL and return an accessibility tree snapshot.

    Uses Camoufox (anti-detection Firefox) by default with a persistent profile
    at ~/.genesis/camoufox-profile/ so cookies and logins survive across calls.

    IMPORTANT: When using Camoufox for stealth browsing (the default), load the
    stealth-browser skill for anti-detection behavioral rules. The skill covers
    timing, interaction patterns, honeypot avoidance, and per-site guidance.

    Set stealth=False to use Chromium fallback for sites incompatible with
    Camoufox (rare). Chromium uses a separate profile at ~/.genesis/browser-profile/.

    Set remote=True to drive the user's real Chrome over CDP/Tailscale.
    This connects to Chrome running on the user's machine with
    --remote-debugging-port=9222. Real browser = real fingerprint = no detection.
    Collaborate timing is auto-enabled. Use for ATS submissions with aggressive
    anti-bot detection (Ashby, Greenhouse with reCAPTCHA v3).

    cdp_url: Override the CDP endpoint. Default: GENESIS_CDP_URL env var.
    Example: browser_navigate("https://jobs.ashbyhq.com/...", remote=True)

    NOTE: If Cloudflare Turnstile is detected (Camoufox only), this call may
    block for up to ~5 minutes while waiting for human resolution via VNC.
    """
    return await _impl_browser_navigate(url, stealth, remote=remote, cdp_url=cdp_url)


@mcp.tool()
async def browser_click(selector: str) -> dict:
    """Click an element on the current page by CSS selector or text.

    Examples: '#submit-btn', 'text=Sign In', '[data-testid="login"]'

    For form controls (radios, checkboxes): prefer specific selectors like
    'input[name="sponsorship"][value="no"]' over ambiguous 'text=No'.
    If a text= selector matches multiple elements, the click fails with
    an ambiguity error listing the matches.

    Keyboard fallback: if mouse click fails on a form control, the tool
    automatically attempts keyboard activation (focus + Space/Enter).
    For manual keyboard navigation, use browser_press_key with Tab/Space.

    Returns the updated page snapshot after clicking.
    """
    return await _with_tool_timeout(
        _impl_browser_click(selector),
        _TOOL_TIMEOUT_S,
        f"browser_click('{selector}')",
    )


@mcp.tool()
async def browser_fill(selector: str, value: str) -> dict:
    """Fill a form field on the current page.

    Examples: browser_fill('#email', 'user@example.com')

    Per-keystroke typing is active for Camoufox — long strings take
    proportionally longer. The tool timeout scales with string length.
    """
    timeout = min(max(60.0, len(value) * 0.25), 300.0)
    return await _with_tool_timeout(
        _impl_browser_fill(selector, value),
        timeout,
        f"browser_fill('{selector}')",
    )


@mcp.tool()
async def browser_upload(selector: str, file_path: str) -> dict:
    """Upload a file to a file input element on the current page.

    Use for <input type="file"> elements (resume uploads, document attachments).
    The file must exist at the given path.

    Examples: browser_upload('input[type=file]', '/path/to/resume.pdf')
    """
    return await _with_tool_timeout(
        _impl_browser_upload(selector, file_path),
        _TOOL_TIMEOUT_S,
        f"browser_upload('{selector}')",
    )


@mcp.tool()
async def browser_screenshot() -> dict:
    """Take a screenshot of the current page.

    Saves to ~/tmp/genesis_browser_screenshot.png and returns the path.
    Use the Read tool to view the image.
    """
    return await _with_tool_timeout(
        _impl_browser_screenshot(), 30.0, "browser_screenshot"
    )


@mcp.tool()
async def browser_snapshot() -> dict:
    """Return the accessibility tree of the current page.

    Token-efficient alternative to screenshots. Returns structured text
    showing all interactive elements, headings, and content.
    """
    return await _with_tool_timeout(
        _impl_browser_snapshot(), 30.0, "browser_snapshot"
    )


@mcp.tool()
async def browser_run_js(expression: str) -> dict:
    """Execute JavaScript in the browser's console on the current page.

    Runs the expression in the page's V8 engine context, equivalent to
    Chrome DevTools console. Returns the expression result.

    Example: browser_run_js('document.title')
    """
    return await _with_tool_timeout(
        _impl_browser_run_js(expression), _TOOL_TIMEOUT_S, "browser_run_js"
    )


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
async def browser_press_key(key: str, count: int = 1) -> dict:
    """Press a keyboard key on the current page.

    Supports Playwright key names: Tab, Enter, Space, ArrowDown, ArrowUp,
    ArrowLeft, ArrowRight, Escape, Backspace, Delete, and combinations
    like Shift+Tab, Control+a.

    Use count > 1 for repeated presses (e.g., Tab 3 times to advance focus).
    Useful as a fallback when click-based interaction fails on form controls.

    Examples: browser_press_key('Tab', 3), browser_press_key('Space'),
              browser_press_key('ArrowDown'), browser_press_key('Shift+Tab')
    """
    return await _with_tool_timeout(
        _impl_browser_press_key(key, count),
        30.0,
        f"browser_press_key('{key}')",
    )


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
    result = {
        "mode": "collaborate" if enable else "background",
        "timing": "fast (0.5-2s)" if enable else "stealth (1-15s)",
        "vnc_url": vnc_url,
        "note": "Browser is always headed on VNC. Open the URL above to watch/interact.",
    }
    if _is_remote_active():
        result["remote_note"] = (
            "Remote CDP session active — collaborate timing is always used "
            "regardless of this setting (user watching their own screen)."
        )
    return result


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
