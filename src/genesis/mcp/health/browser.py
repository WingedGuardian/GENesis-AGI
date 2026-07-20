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
import math
import os
import random
import re
import signal
import time
from pathlib import Path

from genesis.mcp.health import mcp

logger = logging.getLogger(__name__)

# File-based logging for the entire MCP health server — routes ALL log
# output to ~/tmp/mcp_health.log so it's readable after timeouts.
# The MCP server runs as a CC child process; its stderr is inaccessible.
_mcp_log_dir = Path.home() / "tmp"
if _mcp_log_dir.is_dir():
    _mcp_fh = logging.FileHandler(_mcp_log_dir / "mcp_health.log")
    _mcp_fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(name)s] %(levelname)s %(message)s",
    ))
    _mcp_fh.setLevel(logging.DEBUG)
    # Attach to root logger so ALL genesis.* logs are captured
    logging.getLogger("genesis").addHandler(_mcp_fh)

# Direct file-write debug log for Turnstile — bypasses logging framework
# entirely to guarantee output is visible. The logging.FileHandler approach
# produced empty files despite correct setup (likely a process/import issue).
_TS_LOG_PATH = Path.home() / "tmp" / "turnstile_debug.log"


def _ts_log_write(msg: str) -> None:
    """Write a timestamped line directly to the Turnstile debug log."""
    try:
        from datetime import UTC, datetime

        ts = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
        with open(_TS_LOG_PATH, "a") as f:
            f.write(f"{ts} {msg}\n")
            f.flush()
    except Exception:
        pass  # debug logging must never crash the tool


class _TsLog:
    """Minimal logger-like interface that writes directly to file."""

    @staticmethod
    def info(msg: str, *args: object) -> None:
        _ts_log_write(msg % args if args else msg)

    debug = info
    warning = info


_ts_log = _TsLog()

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

# Layer 4: TinyFish cloud browser (on-demand CDP, paid credits)
_tinyfish_pw = None  # Playwright instance for TinyFish session
_tinyfish_browser = None  # CDP Browser connection to TinyFish
_tinyfish_page = None  # Active page
_tinyfish_session_id: str | None = None  # For cleanup via DELETE

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
_VNC_PASSWORD = os.environ.get("GENESIS_VNC_PASSWORD", "genesis")
# vncdotool server format: display-number notation (display 99 = port 5999).
# "localhost::5999" causes Connection Lost due to IPv6 resolution.
_VNC_SERVER = "127.0.0.1:99"

# VNC infrastructure state — verified once per session
_vnc_verified = False

# Cloudflare challenge detection constants (FlareSolverr-proven)
_CHALLENGE_TITLES = ["just a moment", "ddos-guard"]
_CHALLENGE_SELECTORS = [
    'iframe[src*="challenges.cloudflare.com"]',
    'input[name="cf-turnstile-response"]',
    "#cf-challenge-running",
    "#challenge-spinner",
    "#turnstile-wrapper",
    "#cf-please-wait",
    # NOTE: .ray_id excluded — appears on non-challenge Cloudflare error
    # pages (403, 502, 520) and would cause false-positive blocking.
]


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
    processes that survive timeout are caught by the process reaper (hourly at :15).
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

    # TinyFish: terminate cloud session (stops credit burn)
    await _cleanup_tinyfish()

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
            _remote_browser = await asyncio.wait_for(
                _remote_pw.chromium.connect_over_cdp(url), timeout=30.0
            )
        except TimeoutError:
            await _remote_pw.stop()
            _remote_pw = None
            raise ConnectionError(
                f"CDP connection to {url} timed out after 30s. "
                "The remote machine may be asleep or unreachable.\n\n"
                "Check:\n"
                "  1. The machine is awake and on Tailscale\n"
                "  2. Chrome is running with --remote-debugging-port=9222"
            ) from None
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

        # Find user's existing visible tab — never create phantom windows.
        # connect_over_cdp() may create its own default context whose pages
        # render in an invisible off-screen window.  Scan ALL contexts for a
        # real Chrome page (chrome://newtab, about:blank, or any http(s) URL)
        # and prefer that over Playwright's auto-created context.
        _remote_page = None
        for ctx in _remote_browser.contexts:
            for pg in ctx.pages:
                page_url = pg.url
                if page_url.startswith(("chrome://", "about:", "http://", "https://")):
                    _remote_page = pg
                    logger.info("CDP remote connected — using existing tab: %s", page_url)
                    break
            if _remote_page is not None:
                break

        if _remote_page is None:
            # No existing tab found — create in first available context
            contexts = _remote_browser.contexts
            if contexts:
                _remote_page = await contexts[0].new_page()
                logger.info("CDP remote connected — created new tab")
            else:
                ctx = await _remote_browser.new_context()
                _remote_page = await ctx.new_page()
                logger.info("CDP remote connected — created new context and tab")

        return _remote_page


async def _cleanup_tinyfish():
    """Clean up TinyFish browser session (terminate to stop credit burn).

    Timeouts match the existing user-approved 10s pattern in async_cleanup().
    """
    global _tinyfish_pw, _tinyfish_browser, _tinyfish_page, _tinyfish_session_id

    if _tinyfish_browser is not None:
        try:
            await asyncio.wait_for(_tinyfish_browser.close(), timeout=10.0)
        except TimeoutError:
            logger.warning("TinyFish browser close timed out (10s)")
        except Exception:
            logger.debug("TinyFish browser cleanup failed", exc_info=True)
        _tinyfish_browser = None

    if _tinyfish_pw is not None:
        try:
            await asyncio.wait_for(_tinyfish_pw.stop(), timeout=10.0)
        except TimeoutError:
            logger.warning("TinyFish Playwright stop timed out (10s)")
        except Exception:
            logger.debug("TinyFish Playwright cleanup failed", exc_info=True)
        _tinyfish_pw = None

    _tinyfish_page = None

    # Terminate the remote session to stop credit consumption
    if _tinyfish_session_id is not None:
        try:
            from genesis.providers.tinyfish_client import browser_session_delete

            await asyncio.wait_for(
                browser_session_delete(_tinyfish_session_id), timeout=10.0,
            )
            logger.info("TinyFish session %s terminated", _tinyfish_session_id[:12])
        except TimeoutError:
            logger.warning(
                "TinyFish session %s DELETE timed out (10s)",
                _tinyfish_session_id[:12],
            )
        except Exception:
            logger.warning(
                "Failed to terminate TinyFish session %s",
                _tinyfish_session_id[:12],
                exc_info=True,
            )
        _tinyfish_session_id = None


async def _ensure_tinyfish_browser(url: str | None = None) -> tuple:
    """Create a TinyFish cloud browser session and connect via CDP.

    Returns (page, is_new_session). When is_new_session is True and url was
    provided, the page has already navigated to the URL (skip goto).

    Always call _cleanup_tinyfish() when done to terminate the session
    and stop credit consumption.
    """
    global _tinyfish_pw, _tinyfish_browser, _tinyfish_page, _tinyfish_session_id

    async with _browser_lock:
        # Already connected and alive — reuse
        if _tinyfish_page is not None and _tinyfish_browser is not None:
            if _tinyfish_browser.is_connected() and _is_page_alive(_tinyfish_page):
                return _tinyfish_page, False
            logger.warning("TinyFish session stale — cleaning up")
            await _cleanup_tinyfish()

        from playwright.async_api import async_playwright

        from genesis.providers.tinyfish_client import browser_session_create

        # Create remote browser session (takes 10-30s)
        logger.info("Creating TinyFish browser session...")
        session = await browser_session_create(url=url)
        _tinyfish_session_id = session["session_id"]
        cdp_url = session["cdp_url"]
        logger.info(
            "TinyFish session %s created — connecting via CDP",
            _tinyfish_session_id[:12],
        )

        _tinyfish_pw = await async_playwright().start()
        try:
            _tinyfish_browser = await _tinyfish_pw.chromium.connect_over_cdp(cdp_url)
        except Exception as e:
            await _tinyfish_pw.stop()
            _tinyfish_pw = None
            # Terminate the session we just created
            try:
                from genesis.providers.tinyfish_client import browser_session_delete

                await browser_session_delete(_tinyfish_session_id)
            except Exception:
                pass
            _tinyfish_session_id = None
            raise ConnectionError(
                f"TinyFish CDP connection failed: {e}"
            ) from e

        # Handle disconnection: clear state and terminate session
        _tinyfish_browser.on("disconnected", lambda: _on_tinyfish_disconnected())

        # TinyFish docs: sleep 2s after connect for startup nav to settle
        await asyncio.sleep(2)

        # Get the page (TinyFish starts with one context, one tab)
        _tinyfish_page = None
        for ctx in _tinyfish_browser.contexts:
            for pg in ctx.pages:
                _tinyfish_page = pg
                break
            if _tinyfish_page is not None:
                break

        if _tinyfish_page is None:
            contexts = _tinyfish_browser.contexts
            if contexts:
                _tinyfish_page = await contexts[0].new_page()
            else:
                ctx = await _tinyfish_browser.new_context()
                _tinyfish_page = await ctx.new_page()

        if url:
            await _tinyfish_page.wait_for_load_state("domcontentloaded")

        logger.info("TinyFish browser ready — session %s", _tinyfish_session_id[:12])
        return _tinyfish_page, True


def _on_tinyfish_disconnected():
    """Handle TinyFish CDP disconnection — clear state, log warning."""
    global _tinyfish_browser, _tinyfish_page, _tinyfish_session_id
    sid = _tinyfish_session_id[:12] if _tinyfish_session_id else "unknown"
    logger.warning("TinyFish CDP disconnected (session %s)", sid)
    _tinyfish_browser = None
    _tinyfish_page = None
    # session_id is intentionally NOT cleared here — async_cleanup()
    # or next _ensure_tinyfish_browser() will attempt DELETE


def _touch():
    """Record browser activity timestamp for idle timeout tracking."""
    global _last_used
    _last_used = time.monotonic()


async def _idle_watcher_loop():
    """Background task: cleanup browser after idle timeout (1 hour).

    Polls every 60s. When the browser has been idle for _IDLE_TIMEOUT_S,
    calls async_cleanup() and exits. CancelledError is the normal shutdown
    path (MCP lifespan exit or explicit cleanup).

    Note: does NOT acquire _browser_lock before cleanup. async_cleanup()
    cancels and awaits _idle_task (this very coroutine), so holding the
    lock here would self-deadlock. Cleanup is safe without the lock because
    it sets _active_page = None atomically at entry and is individually
    guarded throughout.
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


def _parse_ss_listeners(ss_output):
    """Parse `ss -ltnpH` output into a deduped list of (pid, process_name).

    Each listener row ends with e.g. ``users:(("x11vnc",pid=509730,fd=8))``.
    A process listening on both IPv4 and IPv6 appears on two rows with the
    same pid, so dedup by pid (the names are identical).
    """
    holders = {}
    for name, pid_str in re.findall(r'\(\("([^"]+)",pid=(\d+),', ss_output):
        try:
            holders[int(pid_str)] = name
        except ValueError:
            continue
    return list(holders.items())


def _parse_fuser_pids(fuser_output):
    """Parse `fuser <port>/tcp` stdout (space-separated PIDs) into deduped ints."""
    pids = []
    seen = set()
    for tok in fuser_output.split():
        try:
            pid = int(tok)
        except ValueError:
            continue
        if pid not in seen:
            seen.add(pid)
            pids.append(pid)
    return pids


def _proc_comm(pid):
    """Return /proc/<pid>/comm (process name) or None if unreadable/gone."""
    try:
        with open(f"/proc/{pid}/comm") as fh:
            return fh.read().strip()
    except OSError:
        return None


def _reclaim_vnc_port(port=5999, unit="genesis-vnc"):
    """Safely reclaim a VNC port held by a stale x11vnc process.

    Only SIGKILLs a *foreign* x11vnc process (pid != the unit's MainPID) that
    holds the port while the systemd unit is genuinely down.  Never acts when
    the unit is active/activating (legitimate owner or mid-startup), never
    touches a non-x11vnc process, never blanket-kills the port, and never
    kills pid <= 1.  Prefers `ss` (atomic name+pid, no TOCTOU); falls back to
    `fuser` + /proc/<pid>/comm.  Best-effort: any failure leaves the port as-is.
    Sync (runs in an executor).
    """
    import subprocess as _sp

    # 1. Identify holders (pid + process name when ss is available).
    holders = None
    try:
        r = _sp.run(
            ["ss", "-ltnpH", f"sport = :{port}"],
            capture_output=True, text=True, timeout=3,
        )
        holders = _parse_ss_listeners(r.stdout)
    except FileNotFoundError:
        pass  # ss absent — fall back to fuser
    except Exception:
        logger.debug("ss VNC-port probe failed", exc_info=True)
        return
    if holders is None:
        try:
            r = _sp.run(
                ["fuser", f"{port}/tcp"],
                capture_output=True, text=True, timeout=3,
            )
            holders = [(pid, None) for pid in _parse_fuser_pids(r.stdout)]
        except FileNotFoundError:
            logger.warning(
                "Neither ss nor fuser available — cannot reclaim VNC port %d", port,
            )
            return
        except Exception:
            logger.debug("fuser VNC-port probe failed", exc_info=True)
            return
    if not holders:
        return  # nothing holds the port

    # 2. Never disturb a live or starting unit. Treat the state as UNVERIFIABLE
    #    — and fail SAFE (leave the port as-is) — if the probe raises OR returns
    #    nonzero/empty. The systemd bus being down is the key case: `systemctl`
    #    exits 1 with EMPTY stdout and does NOT raise, so an exception-only guard
    #    would sail past it. A genuinely-down unit returns rc=0 with a real state
    #    ("inactive"), so this never blocks a legitimate reclaim.
    try:
        probe = _sp.run(
            ["systemctl", "--user", "show", "-p", "ActiveState", "--value", unit],
            capture_output=True, text=True, timeout=3,
        )
    except Exception:
        logger.debug("VNC unit ActiveState probe raised — leaving port as-is", exc_info=True)
        return
    state = probe.stdout.strip()
    if probe.returncode != 0 or not state:
        logger.debug(
            "VNC unit ActiveState unverifiable (rc=%s) — leaving port as-is",
            probe.returncode,
        )
        return
    if state in ("active", "activating"):
        return

    # 3. Never kill the unit's own MainPID. Same fail-safe on raise/nonzero/empty.
    #    A down unit returns rc=0 stdout="0" (MainPID 0) — valid, so proceed.
    try:
        probe = _sp.run(
            ["systemctl", "--user", "show", "-p", "MainPID", "--value", unit],
            capture_output=True, text=True, timeout=3,
        )
    except Exception:
        logger.debug("VNC unit MainPID probe raised — leaving port as-is", exc_info=True)
        return
    mp_raw = probe.stdout.strip()
    if probe.returncode != 0 or not mp_raw:
        logger.debug(
            "VNC unit MainPID unverifiable (rc=%s) — leaving port as-is",
            probe.returncode,
        )
        return
    try:
        main_pid = int(mp_raw)
    except ValueError:
        logger.debug("VNC unit MainPID not an int (%r) — leaving port as-is", mp_raw)
        return

    killed = False
    for pid, name in holders:
        # pid > 1 guard: os.kill(1, ...)/(0, ...) would signal init / the whole
        # process group — catastrophic in a container.
        if pid <= 1 or pid == main_pid:
            continue
        proc_name = name if name is not None else _proc_comm(pid)
        if proc_name != "x11vnc":
            continue  # only reclaim from a stale x11vnc — never a bystander
        try:
            os.kill(pid, signal.SIGKILL)
            killed = True
            logger.info("Killed stale x11vnc pid %d holding VNC port %d", pid, port)
        except (ProcessLookupError, PermissionError):
            continue  # already gone or not ours to signal

    if killed:
        time.sleep(1)  # brief grace so systemd can rebind the freed port


async def _ensure_vnc():
    """Verify x11vnc + websockify are running for local browser VNC access.

    Uses systemd services (genesis-vnc, genesis-novnc) as primary mechanism.
    Falls back to raw subprocess if systemctl fails.  Only marks verified
    on confirmed success — retries on next call if setup failed.
    """
    global _vnc_verified
    if _vnc_verified:
        return

    started = False

    def _check_and_start():
        nonlocal started
        import subprocess as _sp

        # Reclaim a stale VNC port safely: only a foreign x11vnc holder when
        # the genesis-vnc unit is genuinely down — never the live unit, an
        # 'activating' (mid-startup) unit, or an unrelated process.
        _reclaim_vnc_port(5999, "genesis-vnc")

        try:
            r = _sp.run(
                ["systemctl", "--user", "is-active", "genesis-vnc"],
                capture_output=True, text=True, timeout=3,
            )
            if r.stdout.strip() == "active":
                started = True
                return
            _sp.run(
                ["systemctl", "--user", "start", "genesis-vnc", "genesis-novnc"],
                capture_output=True, timeout=5,
            )
            # Verify it actually started
            r2 = _sp.run(
                ["systemctl", "--user", "is-active", "genesis-vnc"],
                capture_output=True, text=True, timeout=3,
            )
            if r2.stdout.strip() == "active":
                started = True
                logger.info("Started genesis-vnc + genesis-novnc via systemctl")
        except Exception:
            # Fallback: start x11vnc directly if systemctl unavailable
            try:
                import subprocess as _sp2

                vnc_passwd = Path.home() / ".genesis" / "vnc_passwd"
                auth_arg = (
                    ["-rfbauth", str(vnc_passwd)]
                    if vnc_passwd.exists()
                    else ["-nopw"]
                )
                _sp2.Popen(
                    ["x11vnc", "-display", _VNC_DISPLAY, "-forever", "-shared",
                     "-rfbport", "5999", "-bg"] + auth_arg,
                    stdout=_sp2.DEVNULL, stderr=_sp2.DEVNULL,
                )
                started = True
                logger.info("Started x11vnc directly (systemctl fallback)")
            except FileNotFoundError:
                logger.debug("x11vnc not installed — VNC click unavailable")

    # Run blocking subprocess calls off the event loop
    await asyncio.get_running_loop().run_in_executor(None, _check_and_start)

    if started:
        _vnc_verified = True
    else:
        logger.warning("VNC setup failed — will retry on next browser launch")


async def _get_page(
    stealth: bool = True,
    remote: bool = False,
    cdp_url: str | None = None,
    tinyfish: bool = False,
    tinyfish_url: str | None = None,
):
    """Get the appropriate browser page based on mode.

    Default (stealth=True): Camoufox (anti-detection, primary).
    Plain (stealth=False): Chromium fallback for Camoufox-incompatible sites.
    Remote (remote=True): User's real Chrome via CDP over Tailscale.
    TinyFish (tinyfish=True): Cloud-hosted CDP browser (paid credits).

    Sets _active_page so subsequent interaction tools (click, fill, etc.)
    use whichever browser was last navigated.

    Returns (page, is_new_tinyfish_session) — is_new_tinyfish_session is True
    only when a fresh TinyFish session was just created (URL already loaded).
    """
    global _active_page
    is_new_tinyfish = False
    if tinyfish:
        _active_page, is_new_tinyfish = await _ensure_tinyfish_browser(url=tinyfish_url)
    elif remote:
        _active_page = await _ensure_remote_cdp(cdp_url)
    elif stealth:
        await _ensure_vnc()
        _active_page = await _ensure_browser()
    else:
        await _ensure_vnc()
        _active_page = await _ensure_chromium_fallback()
    _touch()
    _start_idle_watcher()
    return _active_page, is_new_tinyfish


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
    """Returns error dict if remote is active but disconnected. None if OK.

    Read-only — does NOT modify global state. Use _detach_dead_remote()
    when you need to clear globals on disconnection.
    """
    if not _is_remote_active():
        return None
    if not _remote_browser_connected():
        return {
            "error": (
                "Remote Chrome connection lost. "
                "Ask the user to restart Chrome with --remote-debugging-port=9222, "
                "then call browser_navigate(url, remote=True) to reconnect."
            )
        }
    return None


def _detach_dead_remote() -> dict | None:
    """Check remote health and clear globals if disconnected.

    Returns error dict if remote was disconnected (globals cleared),
    None if OK or not in remote mode.
    """
    err = _check_remote_health()
    if err is not None:
        global _active_page, _remote_page
        _active_page = None
        _remote_page = None
    return err


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


# Module-level mouse position tracking (Playwright doesn't expose this).
# Updated by _stealth_click and _idle_jitter; used for micro-jitter.
_mouse_pos: dict[str, float] = {"x": 960.0, "y": 540.0}


async def _idle_jitter(page, duration_s: float) -> None:
    """Emit micro-movements during dwell to simulate hand tremor.

    Real cursors produce ±1-3px displacement at ~0.5-1Hz while "still."
    Call this during any dwell period >2s to avoid dead-cursor detection.
    """
    end = time.monotonic() + duration_s
    while time.monotonic() < end:
        await asyncio.sleep(random.uniform(0.8, 2.5))
        if time.monotonic() >= end:
            break
        dx = random.uniform(-3, 3)
        dy = random.uniform(-3, 3)
        new_x = max(0, _mouse_pos["x"] + dx)
        new_y = max(0, _mouse_pos["y"] + dy)
        await page.mouse.move(new_x, new_y, steps=1)
        _mouse_pos["x"] = new_x
        _mouse_pos["y"] = new_y


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


async def _human_scroll(page, pixels: int, *, direction: str = "down") -> None:
    """Scroll with human-like momentum, variance, and occasional back-scroll.

    Emits variable-delta wheel events with decelerating pauses.
    30% chance of a small back-scroll after reaching the target.
    """
    sign = -1 if direction == "up" else 1
    scrolled = 0
    while scrolled < pixels:
        remaining = pixels - scrolled
        delta = min(random.randint(20, 100), remaining)
        await page.mouse.wheel(0, sign * delta)
        scrolled += delta
        # Deceleration: longer pauses as we approach target
        pause = random.uniform(0.05, 0.15) * (1.0 + scrolled / max(pixels, 1))
        await asyncio.sleep(pause)
    # Occasional back-scroll (30% chance)
    if random.random() < 0.3:
        await asyncio.sleep(random.uniform(0.2, 0.5))
        back = random.randint(10, 40)
        await page.mouse.wheel(0, -sign * back)


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
        _mouse_pos["x"] = target_x
        _mouse_pos["y"] = target_y
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

    Camoufox and CDP remote: clears field via fill(""), then types
    per-keystroke with randomized inter-key intervals.  This fires
    the full keydown → keypress/input → keyup event chain per character
    that behavioral detection systems expect from real users.

    Uses per-character randomization via keyboard.type() for true IKI
    jitter (Playwright's page.type delay= is fixed across all chars).

    Chromium fallback (dev/test): atomic page.fill() (no delay overhead).
    """
    if not _is_camoufox_active() and not _is_remote_active():
        await page.fill(selector, value, timeout=10000)
        return

    # Clear field reliably (works on React controlled inputs)
    await page.fill(selector, "", timeout=10000)
    # Click to focus the field
    await page.click(selector, timeout=10000)
    # Type per-keystroke with hold time + flight time (IKI) jitter.
    # Hold time: log-normal, median ~86ms (CMU Keystroke Dynamics calibration).
    # Flight time: 50-200ms uniform with 5% thinking pauses.
    for char in value:
        # Hold phase: keydown → hold → keyup
        hold_s = random.lognormvariate(math.log(0.086), 0.35)
        hold_s = max(0.03, min(hold_s, 0.20))  # clamp 30-200ms
        await page.keyboard.down(char)
        await asyncio.sleep(hold_s)
        await page.keyboard.up(char)
        # Flight phase: gap to next key
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
                f"Browser at {page_url} hit a Cloudflare challenge. "
                f"Auto-resolve and VNC click both failed after multiple attempts. "
                f"Genesis will retry on next navigation.\n\n"
                f"VNC available at: {vnc_url}"
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


async def _click_turnstile_widget(page) -> bool:
    """Click the Turnstile/managed challenge widget via DOM selectors.

    Primary solver — finds the challenge container on the page and clicks
    at the checkbox position using page.mouse.click(). Camoufox's Juggler
    protocol sends clicks through Firefox's native input handlers, which
    Cloudflare cannot detect as synthetic.

    Strategy 1: Find the CF iframe element on the parent page and click
    within its bounding box (works when iframe has a src attribute).

    Strategy 2: Find the inline managed challenge widget via CSS selectors
    (works for Medium, most CF managed challenges where the widget is
    rendered directly in the page DOM).

    No VNC, no coordinates, no xdotool, no port numbers needed.
    """
    try:
        # Strategy 1: Iframe element on parent page (by src attribute)
        iframe_el = await page.query_selector(
            'iframe[src*="challenges.cloudflare.com"]'
        )
        if iframe_el:
            box = await iframe_el.bounding_box()
            if box:
                click_x = box["x"] + box["width"] / 9
                click_y = box["y"] + box["height"] / 2
                _ts_log.info(
                    "IFRAME CLICK: (%.1f, %.1f) box=%s", click_x, click_y, box,
                )
                logger.info(
                    "Turnstile iframe click: (%.0f, %.0f) in %dx%d box",
                    click_x, click_y, box["width"], box["height"],
                )
                await asyncio.sleep(random.uniform(0.3, 0.8))
                await page.mouse.click(click_x, click_y)
                return True
            _ts_log.info("iframe element found but bounding_box=None")

        # Strategy 2: Managed challenge selectors (inline widget)
        _managed_selectors = [
            ".cf-turnstile",
            "#turnstile-wrapper",
            '[style*="display: grid"]',
            "#cf-challenge-running",
            'input[name="cf-turnstile-response"]',
        ]
        container = None
        matched_selector = None
        for sel in _managed_selectors:
            container = await page.query_selector(sel)
            if container:
                matched_selector = sel
                _ts_log.info("MANAGED selector matched: %s", sel)
                logger.info("Managed challenge matched selector: %s", sel)
                break

        if not container:
            _ts_log.info("No selector matched (tried %d)", len(_managed_selectors))
            logger.warning(
                "No Turnstile widget found — tried %d selectors",
                len(_managed_selectors),
            )
            return False

        # If we found the hidden input, walk up to its parent container
        if matched_selector == 'input[name="cf-turnstile-response"]':
            handle = await container.evaluate_handle(
                "el => el.closest('.cf-turnstile') || el.parentElement"
            )
            container = handle.as_element()
            if container is None:
                logger.warning("cf-turnstile-response parent is not an element")
                return False

        box = await container.bounding_box()
        if not box:
            _ts_log.info("MANAGED selector %s: bounding_box=None", matched_selector)
            logger.warning(
                "Managed selector %s matched but bounding_box was None",
                matched_selector,
            )
            return False

        # Checkbox is near the left edge of the container
        click_x = box["x"] + 20
        click_y = box["y"] + box["height"] / 2
        _ts_log.info(
            "MANAGED CLICK: (%.1f, %.1f) selector=%s box=%s",
            click_x, click_y, matched_selector, box,
        )
        logger.info(
            "Managed challenge click: (%.0f, %.0f) in %dx%d box at (%.0f, %.0f)",
            click_x, click_y, box["width"], box["height"], box["x"], box["y"],
        )
        await asyncio.sleep(random.uniform(0.3, 0.8))
        await page.mouse.click(click_x, click_y)
        return True
    except Exception as e:
        _ts_log.info("WIDGET CLICK EXCEPTION: %s: %s", type(e).__name__, e)
        logger.warning("Turnstile widget click failed: %s", e)
        return False


async def _solve_with_playwright_captcha(page) -> bool:
    """Solve Cloudflare challenge using playwright-captcha library (fallback).

    Uses Shadow DOM traversal via add_init_script to unlock closed shadow roots.
    Explicitly supports Camoufox via FrameworkType.CAMOUFOX.
    Falls back to False if the library is not installed or fails.
    """
    try:
        from playwright_captcha import CaptchaType, ClickSolver
        from playwright_captcha.types import FrameworkType

        solver = ClickSolver(
            framework=FrameworkType.CAMOUFOX,
            page=page,
        )
        await solver.prepare()

        for captcha_type in [CaptchaType.CLOUDFLARE_INTERSTITIAL, CaptchaType.CLOUDFLARE_TURNSTILE]:
            try:
                result = await solver.solve_captcha(page, captcha_type=captcha_type)
                if result:
                    logger.info(
                        "playwright-captcha solved %s challenge", captcha_type.value,
                    )
                    return True
            except Exception as e:
                logger.warning(
                    "playwright-captcha %s failed: %s", captcha_type.value, e,
                )
                continue

        await solver.cleanup()
        return False
    except ImportError:
        logger.debug("playwright-captcha not installed — skipping")
        return False
    except Exception as e:
        logger.warning("playwright-captcha error: %s", e)
        return False


async def _vnc_click_turnstile(page) -> bool:
    """Click the Turnstile checkbox via VNC trusted input (fallback).

    Uses vncdotool to send a mouse click through the VNC protocol, producing
    real X11 input events with network-realistic timing that passes
    Cloudflare's synthetic event fingerprinting (XTest, CDP are detected).

    Returns True if the click was sent (caller must poll for token afterward).
    """
    try:
        # Calculate screen coordinates from browser position + iframe rect.
        # Uses JS to get the browser's own screen offset and chrome height,
        # avoiding fragile hardcoded pixel offsets.
        # Get REAL window position from xdotool (not spoofed JS screenX/screenY).
        # Camoufox's BrowserForge randomizes window.screenX/screenY for
        # anti-fingerprinting, making JS-based coordinates useless for VNC.
        try:
            xdo = await asyncio.create_subprocess_exec(
                "xdotool", "getactivewindow", "getwindowgeometry",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ, "DISPLAY": _VNC_DISPLAY},
            )
            xdo_out, _ = await asyncio.wait_for(xdo.communicate(), timeout=3)
            xdo_text = xdo_out.decode()
            # Parse "Position: X,Y (screen: 0)\n  Geometry: WxH"
            import re
            pos_match = re.search(r"Position:\s*(\d+),(\d+)", xdo_text)
            if pos_match:
                win_x, win_y = int(pos_match.group(1)), int(pos_match.group(2))
            else:
                win_x, win_y = 0, 0
        except Exception:
            win_x, win_y = 0, 0
            logger.debug("xdotool failed — using (0,0) for window position")

        # Get element position from page coordinates (these are NOT spoofed).
        # Try multiple selectors — same set used in _click_turnstile_widget().
        page_coords = await page.evaluate("""() => {
            const iframe = document.querySelector(
                'iframe[src*="challenges.cloudflare"]'
            );
            if (iframe) {
                const rect = iframe.getBoundingClientRect();
                return { left: rect.left + 28, top: rect.top + rect.height / 2,
                         matched: 'iframe' };
            }
            const selectors = [
                '.cf-turnstile', '#turnstile-wrapper',
                '[style*="display: grid"]', '#cf-challenge-running',
            ];
            for (const sel of selectors) {
                const el = document.querySelector(sel);
                if (el) {
                    const rect = el.getBoundingClientRect();
                    return { left: rect.left + 20, top: rect.top + rect.height / 2,
                             matched: sel };
                }
            }
            const input = document.querySelector('input[name="cf-turnstile-response"]');
            if (input) {
                const parent = input.closest('.cf-turnstile') || input.parentElement;
                if (parent) {
                    const rect = parent.getBoundingClientRect();
                    return { left: rect.left + 20, top: rect.top + rect.height / 2,
                             matched: 'cf-turnstile-response parent' };
                }
            }
            return null;
        }""")
        if page_coords is None:
            _ts_log.info("VNC: no element found by any JS selector")
            logger.warning(
                "VNC click: no element found by any selector — "
                "cannot determine click coordinates"
            )
            return False

        # Measure chrome height dynamically instead of hardcoding.
        # BrowserForge spoofs outerHeight/innerHeight but the DIFFERENCE
        # (chrome height) should be preserved since both get the same offset.
        try:
            dims = await page.evaluate("""() => ({
                innerH: window.innerHeight,
                outerH: window.outerHeight,
                dpr: window.devicePixelRatio,
            })""")
            chrome_h = max(0, dims["outerH"] - dims["innerH"])
            dpr = dims.get("dpr", 1.0)
            _ts_log.info(
                "VNC: chrome_h=%d (outer=%d - inner=%d) dpr=%.2f",
                chrome_h, dims["outerH"], dims["innerH"], dpr,
            )
            # If chrome_h is unreasonable (BrowserForge mangled it), fall back
            if chrome_h > 200 or chrome_h < 0:
                _ts_log.info("VNC: chrome_h=%d unreasonable, falling back to 34", chrome_h)
                chrome_h = 34
        except Exception:
            chrome_h = 34
            _ts_log.info("VNC: chrome_h measurement failed, using default 34")

        click_x = win_x + int(page_coords["left"])
        click_y = win_y + chrome_h + int(page_coords["top"])

        _ts_log.info(
            "VNC TARGETING: (%d, %d) matched='%s' "
            "win=(%d,%d) chrome=%d page=(%.1f,%.1f)",
            click_x, click_y, page_coords.get("matched", "?"),
            win_x, win_y, chrome_h,
            page_coords["left"], page_coords["top"],
        )
        logger.info(
            "VNC click: targeting (%d, %d) — matched '%s', "
            "win=(%d,%d) chrome=%d page=(%.0f,%.0f)",
            click_x, click_y, page_coords.get("matched", "?"),
            win_x, win_y, chrome_h,
            page_coords["left"], page_coords["top"],
        )

        await asyncio.sleep(random.uniform(0.5, 1.5))

        # VNC move — separate from click (combined calls timeout).
        # Display-number notation: 127.0.0.1:99 = port 5999.
        move_proc = await asyncio.create_subprocess_exec(
            "vncdo", "-s", _VNC_SERVER, "-p", _VNC_PASSWORD,
            "move", str(click_x), str(click_y),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, move_err = await asyncio.wait_for(
                move_proc.communicate(), timeout=8,
            )
        except TimeoutError:
            move_proc.kill()
            logger.warning("VNC move timed out")
            return False
        if move_proc.returncode != 0:
            err_text = move_err.decode()[:200]
            logger.warning("VNC move failed (rc=%d): %s", move_proc.returncode, err_text)
            if "Connection refused" in err_text or "Connection was refused" in err_text:
                global _vnc_verified
                _vnc_verified = False
            return False

        await asyncio.sleep(random.uniform(0.2, 0.5))

        # VNC click at current position
        click_proc = await asyncio.create_subprocess_exec(
            "vncdo", "-s", _VNC_SERVER, "-p", _VNC_PASSWORD,
            "click", "1",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, click_err = await asyncio.wait_for(
                click_proc.communicate(), timeout=8,
            )
        except TimeoutError:
            click_proc.kill()
            logger.warning("VNC click timed out")
            return False
        if click_proc.returncode != 0:
            err_text = click_err.decode()[:200]
            logger.warning("VNC click failed (rc=%d): %s", click_proc.returncode, err_text)
            return False

        logger.info("VNC click sent at (%d, %d)", click_x, click_y)
        return True

    except FileNotFoundError:
        logger.warning(
            "VNC Turnstile click: vncdo binary not found — "
            "install vncdotool: pip install vncdotool"
        )
        return False
    except Exception as e:
        logger.warning("VNC Turnstile click failed: %s", e, exc_info=True)
        return False


async def _detect_challenge(page) -> bool:
    """Detect any Cloudflare challenge (iframe, managed, interstitial).

    Uses FlareSolverr-proven selectors plus page title check.
    Returns True if a challenge is detected.
    """
    # Check DOM selectors (fastest)
    for selector in _CHALLENGE_SELECTORS:
        el = await page.query_selector(selector)
        if el is not None:
            return True

    # Title-based detection for very early interstitials
    title = (await page.title()).lower()
    return any(ct in title for ct in _CHALLENGE_TITLES)


async def _wait_for_turnstile(page, timeout_ms: int = 15000) -> dict | None:
    """Detect and handle Cloudflare challenge (Turnstile or managed).

    Phase 1 (auto-resolve): Polls for ``timeout_ms`` for automatic resolution.

    Phase 2 (VNC click): Sends real mouse clicks through VNC during the
    checkbox window.  If VNC fails (connection refused), repairs VNC infra
    and retries.  Up to 3 rounds of: wait-for-checkbox → click → poll.

    Phase 3 (reload + retry): Reloads the page (triggers different challenge
    variant) then repeats VNC click.

    No human escalation — Genesis handles this itself.

    Returns None if no challenge detected, or a status dict.
    """
    try:
        # Brief delay for SPA-injected widgets to load
        await asyncio.sleep(0.8)

        _ts_log.info("_wait_for_turnstile called — checking for challenge")
        _ts_log.info("Page URL: %s | Title: %s", page.url, await page.title())

        if not await _detect_challenge(page):
            _ts_log.info("_detect_challenge returned False — no challenge found")
            return None

        _ts_log.info("=== CHALLENGE DETECTED — starting resolution ===")
        _ts_log.info("Page title: %s", await page.title())
        _ts_log.info("Page URL: %s", page.url)
        logger.info("Cloudflare challenge detected — waiting for auto-resolve")

        # Phase 1: auto-resolve (3-5s for trusted browsers, 15s max)
        _ts_log.info("PHASE 1: auto-resolve (%.0fs)", timeout_ms / 1000)
        if await _poll_turnstile_token(page, timeout_ms / 1000, 1.0):
            _ts_log.info("RESOLVED: auto")
            logger.info("Challenge auto-resolved")
            await asyncio.sleep(random.uniform(1.0, 3.0))
            return {"status": "resolved", "method": "auto"}

        # Phase 1.5: Widget click (primary — no VNC needed)
        _ts_log.info("PHASE 1.5: widget click")
        logger.info("Trying Turnstile widget click")
        for click_attempt in range(1, 4):
            _ts_log.info("Widget click attempt %d/3", click_attempt)
            if await _click_turnstile_widget(page):
                if await _poll_turnstile_token(page, 10, 1.0):
                    _ts_log.info("RESOLVED: widget_click (attempt %d)", click_attempt)
                    logger.info("Challenge resolved via widget click (attempt %d)", click_attempt)
                    return {"status": "resolved", "method": "iframe_click"}
                if not await _detect_challenge(page):
                    _ts_log.info("RESOLVED: widget_click (challenge gone)")
                    return {"status": "resolved", "method": "iframe_click"}
                _ts_log.info("Widget click %d: sent but not resolved", click_attempt)
                logger.info("Widget click %d sent but not yet resolved", click_attempt)
                await asyncio.sleep(random.uniform(2, 4))
            else:
                _ts_log.info("Widget click: no target found, breaking")
                break  # No widget found — skip remaining attempts

        # Phase 1.75: playwright-captcha (Shadow DOM traversal — secondary)
        _ts_log.info("PHASE 1.75: playwright-captcha")
        logger.info("Trying playwright-captcha Shadow DOM solver")
        if await _solve_with_playwright_captcha(page):
            if await _poll_turnstile_token(page, 10, 1.0):
                _ts_log.info("RESOLVED: playwright_captcha")
                logger.info("Challenge resolved via playwright-captcha")
                return {"status": "resolved", "method": "playwright_captcha"}
            if not await _detect_challenge(page):
                _ts_log.info("RESOLVED: playwright_captcha (challenge gone)")
                return {"status": "resolved", "method": "playwright_captcha"}

        # Phase 2: VNC click — last resort fallback
        _ts_log.info("PHASE 2: VNC click fallback")
        logger.warning(
            "Iframe + playwright-captcha failed — falling back to VNC click",
        )
        vnc_failed_count = 0
        for attempt in range(1, 4):  # up to 3 attempts
            # Brief wait for checkbox to appear (spinner → checkbox cycle)
            for _ in range(5):  # poll every 2s for up to 10s
                if await _poll_turnstile_token(page, 1, 0.5):
                    logger.info(
                        "Challenge resolved during wait (attempt %d)", attempt,
                    )
                    await asyncio.sleep(random.uniform(1.0, 3.0))
                    return {"status": "resolved", "method": "auto_delayed"}

                if not await _detect_challenge(page):
                    logger.info("Challenge page gone — resolved")
                    return {"status": "resolved", "method": "external"}

                await asyncio.sleep(2)

            # Attempt VNC click
            click_ok = await _vnc_click_turnstile(page)
            if click_ok:
                vnc_failed_count = 0  # reset on success
                if await _poll_turnstile_token(page, 15, 1.0):
                    logger.info(
                        "Challenge resolved via VNC click (attempt %d)",
                        attempt,
                    )
                    await asyncio.sleep(random.uniform(1.0, 3.0))
                    return {"status": "resolved", "method": "vnc_click"}
                logger.info(
                    "VNC click %d sent but challenge not yet resolved",
                    attempt,
                )
            else:
                vnc_failed_count += 1
                logger.warning(
                    "VNC click attempt %d failed — repairing VNC infra",
                    attempt,
                )
                # Self-repair: reset VNC verified flag and re-ensure
                global _vnc_verified
                _vnc_verified = False
                await _ensure_vnc()
                if vnc_failed_count >= 2:
                    # VNC is persistently broken — skip to reload
                    logger.warning("VNC persistently failing — skipping to reload")
                    break

        # Phase 3: Reload and retry with fresh VNC clicks
        logger.info("Trying page reload to trigger different challenge variant")
        try:
            await page.reload(wait_until="domcontentloaded", timeout=15000)
            await asyncio.sleep(2)
            if await _poll_turnstile_token(page, 15, 1.0):
                logger.info("Challenge resolved after reload")
                return {"status": "resolved", "method": "reload"}

            # Post-reload VNC click attempts
            for attempt in range(1, 3):
                await asyncio.sleep(5)  # let new challenge render
                if (
                    await _vnc_click_turnstile(page)
                    and await _poll_turnstile_token(page, 15, 1.0)
                ):
                    logger.info(
                        "Challenge resolved via VNC click after reload "
                        "(attempt %d)", attempt,
                    )
                    return {"status": "resolved", "method": "vnc_click_reload"}
        except Exception:
            logger.debug("Reload failed", exc_info=True)

        # Final: send a Telegram notification but keep the result as blocked
        # so the caller knows the challenge was not resolved.
        logger.warning("Challenge NOT resolved after all attempts")
        await _send_turnstile_alert(page.url)
        return {"status": "blocked", "method": "timeout"}
    except Exception as e:
        _ts_log.info(
            "OUTER EXCEPTION in _wait_for_turnstile: %s: %s", type(e).__name__, e,
        )
        logger.warning("Challenge detection error: %s: %s", type(e).__name__, e)
        return None


# Tool implementations (testable without FastMCP)
# ---------------------------------------------------------------------------


async def _impl_browser_navigate(
    url: str,
    stealth: bool = True,
    remote: bool = False,
    cdp_url: str | None = None,
    tinyfish: bool = False,
) -> dict:
    """Navigate to a URL and return the page snapshot."""
    global _collaborate_mode, _remote_last_url
    _touch()
    _ts_log.info("browser_navigate called: url=%s stealth=%s remote=%s tinyfish=%s", url, stealth, remote, tinyfish)

    if tinyfish and remote:
        return {"error": "Cannot use tinyfish and remote simultaneously — pick one."}

    # Auto-enable collaborate timing for remote CDP (user watching their screen)
    if remote and not _collaborate_mode:
        _collaborate_mode = True
        logger.info("Auto-enabled collaborate timing for remote CDP session")

    try:
        page, is_new_tinyfish = await _get_page(
            stealth, remote=remote, cdp_url=cdp_url,
            tinyfish=tinyfish, tinyfish_url=url if tinyfish else None,
        )
    except ConnectionError as e:
        return {"error": str(e)}
    except ImportError as e:
        return {"error": f"Browser not available: {e}. Install with: pip install playwright"}

    try:
        # Skip goto only when TinyFish session was JUST created with this URL
        # (it already navigated on creation). Subsequent navigations must goto.
        skip_goto = is_new_tinyfish and url
        if not skip_goto:
            _ts_log.info("page.goto starting: %s", url)
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            _ts_log.info("page.goto completed — title: %s", await page.title())

        # Challenge detection for local browsers (Camoufox + Chromium).
        # Skip for TinyFish (cloud browser, clean IP) and remote CDP
        # (user watching their own screen — they can handle challenges).
        turnstile_result = None
        if not tinyfish and not remote:
            _ts_log.info("calling _wait_for_turnstile")
            turnstile_result = await _wait_for_turnstile(page)
            _ts_log.info("_wait_for_turnstile returned: %s", turnstile_result)

        # Track URL for drift detection on remote sessions
        if remote:
            _remote_last_url = page.url

        snapshot = await _snapshot_page(page)

        def _layer_name():
            if tinyfish:
                return "tinyfish_cdp"
            if _is_remote_active():
                return "remote_cdp"
            if _is_camoufox_active():
                return "camoufox"
            return "chromium"

        result = {
            "url": page.url,
            "title": await page.title(),
            "snapshot": snapshot,
            "layer": _layer_name(),
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
    async with _browser_lock:
        if _active_page is None:
            return {"error": "No page open. Call browser_navigate first."}
        health = _detach_dead_remote()
        if health:
            return health
        drift = _check_page_drift(_active_page) if _is_remote_active() else None
        if drift:
            return {
                "advisory": "Page state changed since last Genesis action.",
                **drift,
                "recommendation": "Call browser_snapshot() to see current page state before acting.",
            }
        page = _active_page
    try:
        await _human_delay()
        await _stealth_click(page, selector)
        # A click may trigger navigation (form submit, link). The stealth mouse
        # path — unlike page.click() — has no navigation auto-wait, so settle
        # briefly to let a nav commit, then best-effort wait for the new
        # document. Fully swallowed: never break a click that already worked.
        await asyncio.sleep(0.3)
        with contextlib.suppress(Exception):
            await page.wait_for_load_state("domcontentloaded", timeout=3000)
        _update_remote_url()  # Click may cause navigation (form submit, link)
        snapshot = await _snapshot_page(page)
        return {"clicked": selector, "url": page.url, "snapshot": snapshot}
    except Exception as e:
        return {"error": f"Click failed on '{selector}': {e}"}


async def _impl_browser_fill(selector: str, value: str) -> dict:
    """Fill a form field on the current page."""
    _touch()
    async with _browser_lock:
        if _active_page is None:
            return {"error": "No page open. Call browser_navigate first."}
        health = _detach_dead_remote()
        if health:
            return health
        drift = _check_page_drift(_active_page) if _is_remote_active() else None
        if drift:
            return {
                "advisory": "Page state changed since last Genesis action.",
                **drift,
                "recommendation": "Call browser_snapshot() to see current page state before acting.",
            }
        page = _active_page
    try:
        await _human_delay()
        await _human_type(page, selector, value)
        _update_remote_url()  # Fill + Enter may cause navigation
        return {"filled": selector, "url": page.url}
    except Exception as e:
        return {"error": f"Fill failed on '{selector}': {e}"}


async def _impl_browser_upload(selector: str, file_path: str) -> dict:
    """Upload a file to a file input element on the current page.

    For remote CDP: file must exist on the Genesis container (Playwright sends
    the file contents over the wire to the remote browser).
    """
    _touch()
    async with _browser_lock:
        if _active_page is None:
            return {"error": "No page open. Call browser_navigate first."}
        health = _detach_dead_remote()
        if health:
            return health
        drift = _check_page_drift(_active_page) if _is_remote_active() else None
        if drift:
            return {
                "advisory": "Page state changed since last Genesis action.",
                **drift,
                "recommendation": "Call browser_snapshot() to see current page state before acting.",
            }
        page = _active_page
    p = Path(file_path)
    if not p.is_file():
        return {"error": f"File not found or not a regular file: {file_path}"}
    try:
        await _human_delay()
        await page.set_input_files(selector, str(p), timeout=10000)
        return {"uploaded": p.name, "selector": selector, "url": page.url}
    except Exception as e:
        return {"error": f"Upload failed on '{selector}': {e}"}


async def _impl_browser_screenshot() -> dict:
    """Take a screenshot of the current page."""
    _touch()
    async with _browser_lock:
        if _active_page is None:
            return {"error": "No page open. Call browser_navigate first."}
        health = _detach_dead_remote()
        if health:
            return health
        page = _active_page
    try:
        _SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        screenshot_path = _SCREENSHOT_DIR / "genesis_browser_screenshot.png"
        await page.screenshot(path=str(screenshot_path))
        return {
            "path": str(screenshot_path),
            "url": page.url,
            "title": await page.title(),
        }
    except Exception as e:
        return {"error": f"Screenshot failed: {e}"}


async def _impl_browser_snapshot() -> dict:
    """Return the accessibility tree snapshot of the current page."""
    _touch()
    async with _browser_lock:
        if _active_page is None:
            return {"error": "No page open. Call browser_navigate first."}
        health = _detach_dead_remote()
        if health:
            return health
        page = _active_page
    try:
        snapshot = await _snapshot_page(page)
        return {"url": page.url, "title": await page.title(), "snapshot": snapshot}
    except Exception as e:
        return {"error": f"Snapshot failed: {e}"}


async def _impl_browser_run_js(expression: str) -> dict:
    """Execute JavaScript on the current page and return the result.

    Runs JS in the browser's V8 engine via Playwright page.evaluate().
    Equivalent to Chrome DevTools console. Expressions are logged for audit.
    """
    _touch()
    async with _browser_lock:
        if _active_page is None:
            return {"error": "No page open. Call browser_navigate first."}
        health = _detach_dead_remote()
        if health:
            return health
        page = _active_page
    try:
        logger.info("browser_run_js: %s", expression[:200])
        result = await page.evaluate(expression)
        _update_remote_url()  # JS may cause navigation
        return {"result": result, "url": page.url}
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
    async with _browser_lock:
        if _active_page is None:
            return {"error": "No page open. Call browser_navigate first."}
        health = _detach_dead_remote()
        if health:
            return health
        page = _active_page
    count = max(1, min(count, 50))
    try:
        for i in range(count):
            if i > 0:
                await asyncio.sleep(random.uniform(0.05, 0.15))
            await page.keyboard.press(key)
        return {"pressed": key, "count": count, "url": page.url}
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
    tinyfish: bool = False,
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

    Set tinyfish=True for a cloud-hosted browser via TinyFish Browser API.
    Fresh isolated Chromium on each session. Paid: 1 credit per 4 minutes.
    Use when local browsers fail anti-bot or you need a clean isolated session.

    cdp_url: Override the CDP endpoint. Default: GENESIS_CDP_URL env var.
    Example: browser_navigate("https://jobs.ashbyhq.com/...", remote=True)

    NOTE: If Cloudflare Turnstile is detected (Camoufox only), this call may
    block for up to ~5 minutes while waiting for human resolution via VNC.
    """
    # Remote CDP: bounded by 30s connect + 30s goto = 60s ceiling.
    # Camoufox: Turnstile VNC resolution can take up to 5 minutes.
    timeout = _TOOL_TIMEOUT_S if remote else 300.0
    return await _with_tool_timeout(
        _impl_browser_navigate(url, stealth, remote=remote, cdp_url=cdp_url, tinyfish=tinyfish),
        timeout,
        "browser_navigate",
    )


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

    Per-keystroke typing is active for Camoufox and CDP remote — long
    strings take proportionally longer. The tool timeout scales with
    string length.
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
