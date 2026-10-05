"""Browser automation tools for genesis-health MCP.

Provides lightweight, on-demand browser tools with lazy initialization.
A browser launches only when the first navigation/interaction tool needs it and
stays warm while used. Each layer (Camoufox, Chromium, remote CDP, TinyFish) has
its own lifecycle: it is reclaimed after an hour without a tool call on it,
a stale page restarts only its own layer, and everything shuts down when the
MCP server exits.

Primary browser: Camoufox (anti-detection Firefox). Persistent profile at
~/.genesis/camoufox-profile/ so cookies, localStorage, and login sessions
survive across MCP restarts. Chromium is the fallback for compatibility.

Token-efficient: returns accessibility tree snapshots (YAML-like text) instead
of raw DOM or screenshots by default.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import logging
import math
import os
import random
import re
import signal
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from genesis.browser.types import BrowserLayer
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
        # Deliberate LOCAL import despite the module-level one: it keeps this
        # writer out of reach of patch.object(browser, "datetime"), which the
        # clock-controlled screenshot tests use. Deleting it as "redundant"
        # would let this function consume an injected side_effect entry and
        # write a MagicMock repr into the log — silently, since this block
        # swallows exceptions.
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


class CamoufoxEngineNotReady(RuntimeError):
    """Camoufox or its pinned engine is not installed; launching would download."""


_BROWSER_DISTS = ("camoufox", "playwright", "patchright")


def _installed_browser_versions() -> dict[str, str | None]:
    from importlib.metadata import PackageNotFoundError, version

    found: dict[str, str | None] = {}
    for dist in _BROWSER_DISTS:
        try:
            found[dist] = version(dist)
        except PackageNotFoundError:
            found[dist] = None
    return found


# What was on disk when this process started. A provisioning run that changes
# these packages under a live session leaves any OLD module this process already
# imported in sys.modules (playwright arrives with the first web fetch, camoufox
# with the first launch). The new files then fail in ways reinstalling cannot
# fix (an ImportError, or old camoufox looking for its engine where the new
# layout moved it): only a restart loads the new versions.
_STARTUP_BROWSER_VERSIONS = _installed_browser_versions()


class BrowserPackagesChanged(RuntimeError):
    """Browser packages changed on disk after this process imported them."""


# The browser-stack lock (genesis.browser.engine.BROWSER_LOCK_FILE), held SHARED
# while this process has a local browser (Camoufox or Chromium) open, so a
# provisioning run, which takes it EXCLUSIVE, cannot replace packages, copy a
# live profile or swap the engine under it, nor can a launch start mid-upgrade.
_stack_lock_fd: int | None = None


def _hold_stack_lock() -> None:
    global _stack_lock_fd
    if _stack_lock_fd is not None:
        return
    from genesis.browser import engine

    path = engine.BROWSER_LOCK_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o644)
    except OSError:
        logger.warning("could not open the browser-stack lock %s", path, exc_info=True)
        return  # no lock file possible: the provisioning preflight's process check remains
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise CamoufoxEngineNotReady(
            "the browser stack is being upgraded right now (scripts/install_browser_stack.sh "
            "is running); try again when it finishes"
        ) from None
    except OSError:
        os.close(fd)
        logger.warning("could not lock %s", path, exc_info=True)
        return
    _stack_lock_fd = fd


def _release_stack_lock() -> None:
    global _stack_lock_fd
    if _stack_lock_fd is not None:
        with contextlib.suppress(OSError):
            os.close(_stack_lock_fd)
        _stack_lock_fd = None


def _local_browser_open() -> bool:
    """True while this process has a local browser (Camoufox or Chromium) up.

    A layer's globals are assigned only once its launch succeeded, so a set
    global means a running browser (or its driver), never a half launch.
    """
    return _stealth_cm is not None or _context is not None or _playwright is not None


def _release_stack_lock_if_no_local() -> None:
    """Release the shared browser-stack lock once NO local browser is open.

    Closing one of two local browsers keeps it: the other still runs on the
    installed packages and engine, which provisioning must not replace.
    """
    if not _local_browser_open():
        _release_stack_lock()


async def _launch_local(launch):
    """Run a local browser launch under the shared browser-stack lock.

    Called by _ensure_browser and _ensure_chromium_fallback around their launch,
    so every caller (the tools and medium.py's direct _ensure_browser) holds it.
    """
    _hold_stack_lock()
    try:
        return await launch()
    except BaseException:
        _release_stack_lock_if_no_local()  # nothing came up, so nothing holds the stack
        raise


def _packages_changed_message() -> str | None:
    now = _installed_browser_versions()
    if now == _STARTUP_BROWSER_VERSIONS:
        return None
    changed = ", ".join(
        f"{d} {_STARTUP_BROWSER_VERSIONS[d]} -> {now[d]}"
        for d in _BROWSER_DISTS
        if now[d] != _STARTUP_BROWSER_VERSIONS[d]
    )
    advice = "Restart this Claude Code session to load them."
    if any(now[d] is None for d in ("camoufox", "playwright")):
        advice += " If the browser is still unavailable after that, run scripts/install_browser_stack.sh."
    else:
        advice += " Do not reinstall."
    return f"Browser packages changed after this session started ({changed}). {advice}"


def _check_loaded_browser_modules() -> None:
    """Refuse a launch on modules that no longer match what is on disk.

    Only a distribution this process has already imported matters: one not yet
    loaded will be imported fresh from the files on disk.
    """
    now = _installed_browser_versions()
    if any(
        d in sys.modules and now[d] != _STARTUP_BROWSER_VERSIONS[d] for d in _BROWSER_DISTS
    ):
        raise BrowserPackagesChanged(_packages_changed_message())


def _import_error_message(e: ImportError) -> str:
    changed = _packages_changed_message()
    if changed:
        return f"{changed} (import failed: {e})"
    return (
        f"Browser not available: {e}. "
        "Run scripts/install_browser_stack.sh to install the browser stack."
    )

_PROFILE_DIR = Path.home() / ".genesis" / "camoufox-profile"
_CHROMIUM_PROFILE_DIR = Path.home() / ".genesis" / "browser-profile"

# Module-level browser state — persists across tool calls within a session.
# Layer numbers match genesis.browser.types.BrowserLayer.
# Layer 2: Chromium fallback (patchright)
_playwright = None
_context = None
_page = None
# Layer 1: Camoufox (default)
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
# CDP target id of the tab Genesis opened in the user's Chrome. Survives a
# disconnect and cleanup on purpose: the tab is never closed (owner ruling), so
# a reconnect finds it again and reuses it instead of opening another one.
_remote_target_id: str | None = None

# Layer 4: TinyFish cloud browser (on-demand CDP, paid credits)
_tinyfish_pw = None  # Playwright instance for TinyFish session
_tinyfish_browser = None  # CDP Browser connection to TinyFish
_tinyfish_page = None  # Active page
_tinyfish_session_id: str | None = None  # For cleanup via DELETE

# Collaborative mode — when True, browser launches headed on virtual display :99.
# User watches/interacts via noVNC at http://<tailscale-ip>:6080/vnc.html
_collaborate_mode = False

# Idle timeout — each layer is reclaimed after 1 hour with no tool call ON THAT
# LAYER. User-approved value (2026-04-21). Background asyncio task polls every
# 60s. One timestamp per layer: with a single shared one, any call on any layer
# kept every other layer alive, so a browser left behind on a layer switch
# (Camoufox → remote CDP) never idled out, and a paid TinyFish session billed on
# (#2874). Keyed by BrowserLayer; a layer is absent until first used.
_layer_last_used: dict[BrowserLayer, float] = {}
_idle_task: asyncio.Task | None = None
_IDLE_TIMEOUT_S = 3600  # 1 hour

_SCREENSHOT_DIR = Path.home() / "tmp"
_VNC_DISPLAY = ":99"
#: Pointer readback tolerance. VNC positioning is not exact to the pixel, so a
#: small delta is normal; a large one means the move was not DELIVERED as
#: asked — clamped, dropped, or the pointer grabbed — rather than jitter.
_POINTER_DRIFT_TOLERANCE_PX = 3
#: Pointer readback probe timeout. Generous — xdotool answers in
#: milliseconds against a healthy X server, so exceeding this means the
#: server is wedged, not that the probe was slow.
_POINTER_PROBE_TIMEOUT_S = 5
_VNC_PASSWORD = os.environ.get("GENESIS_VNC_PASSWORD", "genesis")
# vncdotool server format: display-number notation (display 99 = port 5999).
# "localhost::5999" causes Connection Lost due to IPv6 resolution.
_VNC_SERVER = "127.0.0.1:99"

# VNC infrastructure state — verified once per session
_vnc_verified = False

# Cloudflare challenge detection constants (FlareSolverr-proven)
_CHALLENGE_TITLES = ["just a moment", "ddos-guard"]
# Interstitial (blocking full-page challenge) markers. A Cloudflare interstitial
# INTERCEPTS the request and replaces the page; these appear only on the challenge
# chrome, never on an embedded widget. The strongest signal is the cf-mitigated
# response header (checked in _detect_interstitial).
# NOTE: .ray_id excluded — appears on non-challenge CF error pages (403/502/520).
_INTERSTITIAL_SELECTORS = [
    "#cf-challenge-running",
    "#challenge-spinner",
    "#cf-please-wait",
]
# Embedded Turnstile widget markers. These appear on ordinary, already-loaded
# pages (login/signup forms) that merely EMBED a widget — NOT a blocking
# challenge. Detected separately (short-grace path), never escalated to VNC/alert.
_WIDGET_SELECTORS = [
    'iframe[src*="challenges.cloudflare.com"]',
    'input[name="cf-turnstile-response"]',
    "#turnstile-wrapper",
    ".cf-turnstile",
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


def _layer_page(layer: BrowserLayer):
    """The page a layer currently drives (None when the layer is closed)."""
    return {
        BrowserLayer.CAMOUFOX: _stealth_page,
        BrowserLayer.CHROMIUM: _page,
        BrowserLayer.REMOTE_CDP: _remote_page,
        BrowserLayer.TINYFISH: _tinyfish_page,
    }[layer]


def _layer_of(page) -> BrowserLayer | None:
    """Which layer drives ``page`` (by identity), or None."""
    if page is None:
        return None
    for layer in BrowserLayer:
        if _layer_page(layer) is page:
            return layer
    return None


def _layer_open(layer: BrowserLayer) -> bool:
    """True while a layer holds anything its cleanup must release.

    Wider than "has a live page" on purpose: a dropped CDP connection leaves its
    Playwright driver, and a dropped TinyFish connection leaves a session that
    still bills until it is DELETEd.
    """
    if layer is BrowserLayer.CAMOUFOX:
        return _stealth_cm is not None
    if layer is BrowserLayer.CHROMIUM:
        return _context is not None or _playwright is not None
    if layer is BrowserLayer.REMOTE_CDP:
        return _remote_browser is not None or _remote_pw is not None
    return (
        _tinyfish_browser is not None
        or _tinyfish_pw is not None
        or _tinyfish_session_id is not None
    )


def _forget_layer(layer: BrowserLayer, page) -> None:
    """Bookkeeping shared by every layer's cleanup: drop its idle clock, and
    stop the tools pointing at its page."""
    global _active_page
    _layer_last_used.pop(layer, None)
    if page is not None and _active_page is page:
        _active_page = None


async def _cleanup_camoufox() -> None:
    """Close Camoufox (layer 1) and its Playwright driver. Touches no other layer.

    The globals are detached BEFORE the close, so the context's own "close"
    event (see _on_local_context_closed) finds nothing to do. Releases the
    browser-stack lock only if Chromium is not open either.
    """
    global _stealth_cm, _stealth_browser, _stealth_page

    cm, page = _stealth_cm, _stealth_page
    _stealth_cm = None
    _stealth_browser = None
    _stealth_page = None
    _forget_layer(BrowserLayer.CAMOUFOX, page)
    if cm is not None:
        try:
            await asyncio.wait_for(cm.__aexit__(None, None, None), timeout=10.0)
        except TimeoutError:
            logger.warning("Camoufox cleanup timed out (10s)")
        except Exception:
            logger.debug("Camoufox cleanup failed", exc_info=True)
    _release_stack_lock_if_no_local()


async def _cleanup_chromium() -> None:
    """Close the Chromium fallback (layer 2) and stop its driver. Touches no
    other layer; releases the browser-stack lock only if Camoufox is closed too."""
    global _playwright, _context, _page

    pw, ctx, page = _playwright, _context, _page
    _playwright = None
    _context = None
    _page = None
    _forget_layer(BrowserLayer.CHROMIUM, page)
    if ctx is not None:
        try:
            await asyncio.wait_for(ctx.close(), timeout=10.0)
        except TimeoutError:
            logger.warning("Browser context close timed out (10s)")
        except Exception:
            logger.debug("Browser context cleanup failed", exc_info=True)
    if pw is not None:
        try:
            await asyncio.wait_for(pw.stop(), timeout=10.0)
        except TimeoutError:
            logger.warning("Playwright stop timed out (10s) — driver may be orphaned")
        except Exception:
            logger.debug("Playwright cleanup failed", exc_info=True)
    _release_stack_lock_if_no_local()


_LAYER_CLEANUP = {
    BrowserLayer.CAMOUFOX: lambda: _cleanup_camoufox(),
    BrowserLayer.CHROMIUM: lambda: _cleanup_chromium(),
    BrowserLayer.REMOTE_CDP: lambda: _cleanup_remote_cdp(),
    BrowserLayer.TINYFISH: lambda: _cleanup_tinyfish(),
}


def _on_local_context_closed(layer: BrowserLayer, ctx) -> None:
    """A local browser's context closed on its own (its window was closed, or
    the browser exited). Run that layer's cleanup, which stops the Playwright
    driver: without this the driver stayed alive until the next cleanup.

    A close event from a context this module no longer holds (an old launch,
    or our own cleanup, which detaches first) is ignored.
    """
    current = _stealth_browser if layer is BrowserLayer.CAMOUFOX else _context
    if current is None or current is not ctx:
        return
    logger.warning("%s window closed — stopping its driver", layer.value)

    async def _reap():
        async with _browser_lock:
            now = _stealth_browser if layer is BrowserLayer.CAMOUFOX else _context
            if now is ctx:
                await _LAYER_CLEANUP[layer]()

    from genesis.util.tasks import tracked_task

    tracked_task(_reap(), name=f"browser-{layer.value}-closed")


async def async_cleanup():
    """Shut down EVERY browser layer. Called from the MCP lifespan end.

    Idle reclaim and stale-page recovery use the per-layer cleanups instead
    (_cleanup_camoufox, _cleanup_chromium, _cleanup_remote_cdp,
    _cleanup_tinyfish), so they never disconnect remote CDP or end a paid
    TinyFish session that is still in use.

    Safe to call when the browser is already dead — all steps are
    individually guarded so a crashed Camoufox won't hang cleanup.
    Each cleanup step has a 10s timeout (user-approved) to prevent hanging
    if the Playwright Node.js driver or browser process is stuck. Orphaned
    processes that survive timeout are caught by the process reaper (hourly at :15).
    """
    global _active_page, _idle_task

    _active_page = None

    # Cancel the idle watcher first, so it cannot reclaim a layer concurrently.
    if _idle_task is not None:
        _idle_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _idle_task
        _idle_task = None

    # Remote CDP: disconnect (does NOT close user's Chrome)
    await _cleanup_remote_cdp()
    # TinyFish: terminate cloud session (stops credit burn)
    await _cleanup_tinyfish()
    await _cleanup_chromium()
    await _cleanup_camoufox()
    _layer_last_used.clear()
    _opened_by.clear()
    _release_stack_lock()  # no local browser is open any more


async def _ensure_browser():
    """Lazily initialize Camoufox (primary browser) with persistent profile.

    Returns the active page. Raises CamoufoxEngineNotReady when the package or
    its pinned engine is not installed. In collaborate mode, launches headed on
    virtual display :99 for VNC sharing. Uses anti-detection Firefox by default
    for all browsing.

    Detects stale pages (e.g. browser killed by a concurrent session) and
    restarts Camoufox alone: remote CDP and TinyFish are left as they are.
    The launch holds the shared browser-stack lock (_launch_local).
    """
    global _stealth_cm, _stealth_browser, _stealth_page

    async with _browser_lock:
        if _stealth_page is not None and _is_page_alive(_stealth_page):
            return _stealth_page
        if _layer_open(BrowserLayer.CAMOUFOX):
            logger.warning("Camoufox page is stale — restarting Camoufox")
            await _cleanup_camoufox()

        async def _launch():
            global _stealth_cm, _stealth_browser, _stealth_page

            # Refuse BEFORE camoufox runs: its launch path deletes a pre-0.5
            # engine directory and downloads the pinned build from inside this
            # call, raced by every session's MCP process. Provisioning is
            # bootstrap's job.
            from genesis.browser.engine import camoufox_engine_status

            engine = camoufox_engine_status()
            if not engine.ready:
                raise CamoufoxEngineNotReady(engine.detail)
            _check_loaded_browser_modules()

            from camoufox.async_api import AsyncCamoufox

            _PROFILE_DIR.mkdir(parents=True, exist_ok=True)

            # Always headed — Xvfb :99 is always running.
            os.environ["DISPLAY"] = _VNC_DISPLAY

            cm = AsyncCamoufox(
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
            # A failed __aenter__ stops its own driver (camoufox 0.5.7,
            # AsyncCamoufox.__aenter__), and nothing is assigned yet, so a
            # failed launch leaves no half state behind to hold the lock.
            ctx = await cm.__aenter__()
            # With persistent_context, browser IS the context
            _stealth_cm, _stealth_browser = cm, ctx
            ctx.on("close", lambda c: _on_local_context_closed(BrowserLayer.CAMOUFOX, c))
            _stealth_page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            return _stealth_page

        page = await _launch_local(_launch)
        mode_str = "headed (collaborate)" if _collaborate_mode else "headed"
        logger.info(
            "Camoufox browser launched %s with persistent profile at %s", mode_str, _PROFILE_DIR
        )
        return page


async def _ensure_chromium_fallback():
    """Lazily initialize the Chromium fallback browser (patchright).

    Use only when Camoufox fails on a specific site. Persistent profile at
    ~/.genesis/browser-profile/ (separate from Camoufox profile).

    patchright is Playwright with the Chromium automation leaks patched
    (Runtime.enable, Console.enable, automation command-line flags). It is a
    drop-in replacement; plain Playwright is used only when patchright is not
    installed, with a warning, because that Chromium is detectable.

    Detects stale pages and restarts Chromium alone (other layers untouched).
    The launch holds the shared browser-stack lock (_launch_local).
    """
    async with _browser_lock:
        if _page is not None and _is_page_alive(_page):
            return _page
        if _layer_open(BrowserLayer.CHROMIUM):
            logger.warning("Chromium page is stale — restarting Chromium")
            await _cleanup_chromium()

        async def _launch():
            global _playwright, _context, _page

            _check_loaded_browser_modules()
            try:
                from patchright.async_api import async_playwright
            except ImportError:
                import importlib.util

                if importlib.util.find_spec("patchright") is not None:
                    raise  # installed but broken: report it, never switch silently
                logger.warning(
                    "patchright is not installed; the Chromium fallback uses plain "
                    "Playwright, which sites can detect. Run scripts/install_browser_stack.sh."
                )
                from playwright.async_api import async_playwright

            _CHROMIUM_PROFILE_DIR.mkdir(parents=True, exist_ok=True)

            # Always headed — Xvfb :99 is always running.
            os.environ["DISPLAY"] = _VNC_DISPLAY

            pw = await async_playwright().start()
            try:
                ctx = await pw.chromium.launch_persistent_context(
                    user_data_dir=str(_CHROMIUM_PROFILE_DIR),
                    headless=False,
                    args=[
                        "--no-sandbox",
                        "--disable-gpu",
                        "--disable-dev-shm-usage",
                        "--start-maximized",
                    ],
                    # patchright's documented stealth setup: no emulated viewport
                    # (the real window size is used) and no custom headers or user
                    # agent.
                    no_viewport=True,
                )
            except BaseException:
                # Stop the driver this launch started; left running, the next
                # launch would start another and orphan this one.
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(pw.stop(), timeout=10.0)
                raise
            _playwright, _context = pw, ctx
            ctx.on("close", lambda c: _on_local_context_closed(BrowserLayer.CHROMIUM, c))
            _page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            return _page

        page = await _launch_local(_launch)
        mode_str = "headed (collaborate)" if _collaborate_mode else "headed"
        logger.info(
            "Chromium fallback launched %s with profile at %s", mode_str, _CHROMIUM_PROFILE_DIR
        )
        return page
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
    it does NOT terminate the remote Chrome process, and it does not close the
    Genesis tab either (it lives in the user's own context). The tab is left
    open on purpose and ``_remote_target_id`` is kept, so the next connect
    reuses it. Touches no other layer.
    """
    global _remote_pw, _remote_browser, _remote_page, _remote_last_url

    _forget_layer(BrowserLayer.REMOTE_CDP, _remote_page)
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

    Does NOT launch Chrome. Works in a tab of its own (see
    _genesis_remote_tab) and never navigates or closes the user's tabs.
    On disconnect, clears state — next call gets a clear error.
    """
    global _remote_pw, _remote_browser, _remote_page, _remote_cdp_url

    async with _browser_lock:
        # Already connected and alive — reuse
        if (
            _remote_page is not None
            and _remote_browser is not None
            and _remote_browser.is_connected()
            and _is_page_alive(_remote_page)
        ):
            return _remote_page
        # Anything left over (a dropped connection keeps its Playwright driver)
        # is released before connecting again, or it would be orphaned.
        if _layer_open(BrowserLayer.REMOTE_CDP):
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
        try:
            _remote_page = await _genesis_remote_tab(_remote_browser)
        except BaseException as e:
            # Connected but no usable tab (Chrome closed, the context refused a
            # page, or the tool timeout cancelled the call): disconnect and stop
            # this driver now, or the next attempt starts another and the first
            # leaks. _remote_target_id is kept, so a retry still reuses the
            # Genesis tab if it exists. The cleanup is shielded so a cancellation
            # cannot cut it short.
            await asyncio.shield(_cleanup_remote_cdp())
            if not isinstance(e, Exception):
                raise  # cancellation / interpreter exit: propagate as is
            raise ConnectionError(
                f"Connected to Chrome at {url} but could not open the Genesis tab: {e}"
            ) from e
        return _remote_page


async def _cdp_target_id(page) -> str | None:
    """The CDP target id of a Chromium page, or None if it cannot be read."""
    try:
        session = await page.context.new_cdp_session(page)
        try:
            info = await session.send("Target.getTargetInfo")
        finally:
            with contextlib.suppress(Exception):
                await session.detach()
        return info["targetInfo"]["targetId"]
    except Exception:
        # Warning, not debug: a None here means the Genesis tab cannot be found
        # again, so every reconnect would silently open another tab.
        logger.warning("Could not read the CDP target id of a remote tab", exc_info=True)
        return None


async def _genesis_remote_tab(remote_browser):
    """Return Genesis's own tab in the user's Chrome, opening it if needed.

    One Genesis tab per session (owner ruling): the user's tabs are never
    navigated. A tab opened earlier is found again by its CDP target id and
    reused; if the user closed it, a new one is opened. Genesis never closes
    it: the user may still be reading it.

    The tab opens in the context that already holds the user's visible tabs.
    ``browser.new_context()`` is the last resort only, because over CDP its
    pages render in an invisible off-screen window.
    """
    global _remote_target_id

    contexts = list(remote_browser.contexts)
    if _remote_target_id is not None:
        for ctx in contexts:
            # Newest first: the Genesis tab was opened after the user's tabs,
            # so this usually finds it without probing every user tab.
            for pg in reversed(ctx.pages):
                if await _cdp_target_id(pg) == _remote_target_id:
                    logger.info("CDP remote connected, reusing the Genesis tab: %s", pg.url)
                    return pg

    home = next(
        (
            ctx for ctx in contexts
            if any(
                pg.url.startswith(("chrome://", "about:", "http://", "https://"))
                for pg in ctx.pages
            )
        ),
        contexts[0] if contexts else None,
    )
    if home is None:
        logger.warning(
            "Remote Chrome exposes no browser context; opening one, whose tab "
            "may not be visible on the user's screen"
        )
        home = await remote_browser.new_context()
    page = await home.new_page()
    _remote_target_id = await _cdp_target_id(page)
    logger.info("CDP remote connected, opened a Genesis tab (target %s)", _remote_target_id)
    return page


async def _cleanup_tinyfish():
    """Clean up TinyFish browser session (terminate to stop credit burn).

    Timeouts match the existing user-approved 10s pattern in async_cleanup().
    Touches no other layer.
    """
    global _tinyfish_pw, _tinyfish_browser, _tinyfish_page, _tinyfish_session_id

    _forget_layer(BrowserLayer.TINYFISH, _tinyfish_page)
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

    _cleanup_tinyfish() terminates the session and stops credit consumption:
    on a layer switch (_get_page), at idle reclaim, and at async_cleanup.
    """
    global _tinyfish_pw, _tinyfish_browser, _tinyfish_page, _tinyfish_session_id

    async with _browser_lock:
        # Already connected and alive — reuse
        if (
            _tinyfish_page is not None
            and _tinyfish_browser is not None
            and _tinyfish_browser.is_connected()
            and _is_page_alive(_tinyfish_page)
        ):
            return _tinyfish_page, False
        # A dropped connection keeps its driver AND its session id, which bills
        # until DELETEd: end both before creating a new session over them.
        if _layer_open(BrowserLayer.TINYFISH):
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
    global _tinyfish_browser, _tinyfish_page, _tinyfish_session_id, _active_page
    sid = _tinyfish_session_id[:12] if _tinyfish_session_id else "unknown"
    logger.warning("TinyFish CDP disconnected (session %s)", sid)
    if _active_page is not None and _active_page is _tinyfish_page:
        _active_page = None  # the tools report "No page open" instead of a dead page's errors
    _tinyfish_browser = None
    _tinyfish_page = None
    # session_id is intentionally NOT cleared here: _cleanup_tinyfish (the
    # next _ensure_tinyfish_browser, a layer switch, idle reclaim or
    # async_cleanup) still has to DELETE it to stop the billing.


def _touch(layer: BrowserLayer | None = None) -> None:
    """Record activity on ONE layer for idle tracking: ``layer``, or the layer
    of the active page when None. Other layers' clocks keep running."""
    if layer is None:
        layer = _layer_of(_active_page)
    if layer is not None:
        _layer_last_used[layer] = time.monotonic()


async def _reclaim_idle_layers(now: float) -> None:
    """One idle pass: clean up each open layer unused for _IDLE_TIMEOUT_S.

    Each layer is judged on its own clock, so an abandoned Camoufox is reclaimed
    while remote CDP stays in use. An open layer with no clock yet (launched
    outside _get_page, e.g. medium.py's direct _ensure_browser) starts its clock
    now rather than living forever. The cleanup runs under _browser_lock and
    re-checks the clock there, so a tool call that touched the layer meanwhile
    keeps it.
    """
    for layer in BrowserLayer:
        if not _layer_open(layer):
            _layer_last_used.pop(layer, None)
            continue
        last = _layer_last_used.setdefault(layer, now)
        if now - last < _IDLE_TIMEOUT_S:
            continue
        async with _browser_lock:
            last = _layer_last_used.get(layer, now)
            if _layer_open(layer) and now - last >= _IDLE_TIMEOUT_S:
                logger.info(
                    "Browser layer %s idle for %ds — reclaiming it", layer.value, _IDLE_TIMEOUT_S,
                )
                await _LAYER_CLEANUP[layer]()


async def _idle_watcher_loop():
    """Background task: reclaim each browser layer after 1 hour idle on it.

    Polls every 60s and exits once no layer is open (_get_page restarts it).
    CancelledError is the normal shutdown path (async_cleanup at MCP lifespan
    exit). Per-layer cleanups never cancel this task, so it may hold
    _browser_lock while reclaiming without deadlocking itself.
    """
    try:
        while True:
            await asyncio.sleep(60)
            try:
                await _reclaim_idle_layers(time.monotonic())
            except Exception:
                # One failed reclaim must not end idle tracking of the others.
                logger.warning("Browser idle reclaim failed", exc_info=True)
            if not any(_layer_open(layer) for layer in BrowserLayer):
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
            # Fallback: start x11vnc directly if systemctl unavailable.
            # The fallback keeps password authentication, like the unit: with
            # no password file it starts nothing and says why.
            vnc_passwd = Path.home() / ".genesis" / "vnc_passwd"
            if not vnc_passwd.exists():
                logger.warning(
                    "VNC fallback not started: %s is missing, and the fallback "
                    "keeps password authentication. Run scripts/setup-vnc.sh "
                    "to create it.",
                    vnc_passwd,
                )
                return
            try:
                import subprocess as _sp2

                _sp2.Popen(
                    ["x11vnc", "-display", _VNC_DISPLAY, "-forever", "-shared",
                     "-rfbport", "5999", "-bg", "-rfbauth", str(vnc_passwd)],
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

    Moving to another layer ENDS an open TinyFish session (paid per minute),
    once the new layer is up: a failed switch keeps it. Abandoned local
    browsers and remote CDP are left alone (owner ruling); each is reclaimed by
    its own idle clock.
    """
    global _active_page
    layer = _requested_layer(stealth, remote, tinyfish)
    is_new_tinyfish = False
    if layer is BrowserLayer.TINYFISH:
        page, is_new_tinyfish = await _ensure_tinyfish_browser(url=tinyfish_url)
    elif layer is BrowserLayer.REMOTE_CDP:
        page = await _ensure_remote_cdp(cdp_url)
    elif layer is BrowserLayer.CAMOUFOX:
        await _ensure_vnc()
        page = await _ensure_browser()
    else:
        await _ensure_vnc()
        page = await _ensure_chromium_fallback()
    _active_page = page
    # Leaving TinyFish does NOT end its session: ending a paid session because
    # of what it costs is automatic cost control, which the design principles
    # rule out (cost is observed, the user decides). Its own idle clock bounds
    # an abandoned session to the idle window, like every other layer.
    _touch(layer)
    _start_idle_watcher()
    return page, is_new_tinyfish


def _requested_layer(stealth: bool, remote: bool, tinyfish: bool) -> BrowserLayer:
    """The layer a browser_navigate call asks for (same precedence as _get_page)."""
    if tinyfish:
        return BrowserLayer.TINYFISH
    if remote:
        return BrowserLayer.REMOTE_CDP
    return BrowserLayer.CAMOUFOX if stealth else BrowserLayer.CHROMIUM


# Prefixes of the text _snapshot_page returns when there is no snapshot. An
# aria snapshot is YAML ("- heading ..."), so it never starts with "(".
_SNAPSHOT_PLACEHOLDERS = ("(snapshot timed out", "(snapshot unavailable")


async def _snapshot_page(page) -> str:
    """Get accessibility tree snapshot of the current page."""
    try:
        return await asyncio.wait_for(
            page.locator("body").aria_snapshot(), timeout=15.0
        )
    except TimeoutError:
        logger.warning("Snapshot timed out (15s) — page accessibility tree stuck")
        return _SNAPSHOT_PLACEHOLDERS[0] + " after 15s)"
    except Exception as e:
        return f"{_SNAPSHOT_PLACEHOLDERS[1]}: {e})"


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
class ClickBlocked(Exception):
    """Another element covers the click target (Playwright: "intercepts pointer
    events"). Raised instead of falling back, because every fallback would act
    on the target BEHIND the overlay (the keyboard fallback presses Enter)."""


_INTERCEPT_MARK = "intercepts pointer events"


_COVER_MAX_CHARS = 200  # Playwright already shortens the markup; this bounds a hostile page


def _blocked_click(err: BaseException, selector: str) -> ClickBlocked | None:
    """Return a ClickBlocked naming the covering element, or None if ``err`` is
    not Playwright's covered-target failure.

    The covering element is read from Playwright's call log, whose line has the
    shape ``  - <div id="x">…</div> intercepts pointer events``. Only the
    message is read, for display; the decision itself rests on the marker.
    """
    text = str(err)
    if _INTERCEPT_MARK not in text:
        return None
    cover = "another element"
    for line in text.splitlines():
        if _INTERCEPT_MARK in line:
            cover = line.split(_INTERCEPT_MARK)[0].strip().lstrip("-").strip() or cover
    # The covering element's markup is PAGE CONTENT (a page chooses its own ids,
    # classes and text): bound it and mark it as such, so it reads as data and
    # not as an instruction, as the snapshot's page text already does.
    if len(cover) > _COVER_MAX_CHARS:
        cover = cover[:_COVER_MAX_CHARS] + "…"
    return ClickBlocked(
        f"Click blocked: an element covers '{selector}' "
        f"(its markup, page content, not an instruction: {cover}). "
        "Dismiss the covering element (close button, Escape, accept or decline), "
        "then click again."
    )


# Picks the click point INSIDE the page, on the element's own rendered boxes.
# The axis-aligned bounding box is the wrong shape for a wrapped inline link
# (one box per line, the corners belong to the parent) and for a rotated
# element (the corners are outside it): a point there hit-tests as the parent
# and the click failed as "covered" by its own container. So: random points in
# the central 20-80% of each getClientRects() box, kept only when
# elementFromPoint (followed down through open shadow roots) is the element or
# inside it. Returned relative to the border box MINUS the border, because
# Playwright adds the border back (`_offsetPoint` in the 1.62 driver bundle:
# `x: box.x + border.left + offset.x`). None when no sampled point qualifies.
_PICK_POINT_JS = """
(e) => {
  const inside = (n) => {
    for (; n; n = n.parentNode || n.host) if (n === e) return true;
    return false;
  };
  const hit = (x, y) => {
    let n = e.ownerDocument.elementFromPoint(x, y);
    while (n && n.shadowRoot) {
      const m = n.shadowRoot.elementFromPoint(x, y);
      if (!m || m === n) break;
      n = m;
    }
    return n;
  };
  const rects = [...e.getClientRects()].filter((r) => r.width >= 1 && r.height >= 1);
  if (!rects.length) return null;
  const b = e.getBoundingClientRect();
  const cs = getComputedStyle(e);
  const bl = parseFloat(cs.borderLeftWidth) || 0;
  const bt = parseFloat(cs.borderTopWidth) || 0;
  for (let i = 0; i < 24; i++) {
    const r = rects[i % rects.length];
    const x = r.left + r.width * (0.2 + 0.6 * Math.random());
    const y = r.top + r.height * (0.2 + 0.6 * Math.random());
    if (inside(hit(x, y))) return { x: x - b.left - bl, y: y - b.top - bt, bl, bt };
  }
  return null;
}
"""

# How a form control relates to its <label>. Used when the control itself
# cannot be clicked: a styled checkbox/radio hides its <input> (zero size,
# display:none) or covers it with its own decoration (<span class=mark>). The
# label is the control's own click target then, so clicking it is not pushing
# through an overlay. "wrap": the label is an ancestor; "for": label[for=id].
_LABEL_JS = """
(e) => {
  const r = e.getBoundingClientRect();
  const visible = r.width > 0 && r.height > 0 && getComputedStyle(e).visibility !== 'hidden';
  const label = e.labels && e.labels.length ? e.labels[0] : null;
  if (!label) return { visible, label: null };
  if (label.contains(e)) return { visible, label: 'wrap' };
  return { visible, label: 'for', id: e.id || '' };
}
"""

# Playwright's call log line for a pointer action that was actually sent:
# `progress3.log(`  performing ${actionName} action`)` in _performPointerAction
# (Playwright 1.62 driver, lib/coreBundle.js). Anything that fails before it
# (not attached, not visible, not stable, covered at the pre-check) sent
# nothing; anything after it may have been delivered.
_CLICK_SENT_MARK = "performing click action"


def _click_was_sent(err: BaseException) -> bool:
    return _CLICK_SENT_MARK in str(err)


def _label_locator(page, loc, info: dict):
    """A Locator for the control's label (re-resolved per call, like the
    control's own), or None."""
    if info.get("label") == "wrap":
        return loc.locator("xpath=ancestor::label[1]")
    if info.get("label") == "for" and info.get("id"):
        ident = info["id"].replace("\\", "\\\\").replace('"', '\\"')
        return page.locator(f'label[for="{ident}"]').first
    return None


async def _humanized_click(page, target, timeout: int, label=None) -> None:
    """Scroll ``target`` (a Locator) into view, move the mouse to a point on it
    with Camoufox's humanized trail, dwell, then click that point.

    ``label`` is the control's <label> Locator, if any. When no sampled point
    on the control hit-tests as the control itself (a styled checkbox whose
    <input> sits under its own <span class=mark>), the label is clicked
    instead: that is where a person clicks, and the decoration is part of the
    control, not an overlay. A real overlay still covers the label too, so the
    label's click then fails as ClickBlocked naming it.

    The click is NOT ``no_wait_after``: Playwright reads the hit-target
    interceptor's verdict only when it waits after the action (`if
    (options.waitAfter !== false)` around `stopHitTargetInterception`, 1.62
    bundle), so with no_wait_after a click swallowed by an overlay that appears
    during the move returned success with nothing clicked.
    """
    await target.scroll_into_view_if_needed(timeout=timeout)
    pos = await target.evaluate(_PICK_POINT_JS)
    if pos is None and label is not None:
        target = label
        await target.scroll_into_view_if_needed(timeout=timeout)
        pos = await target.evaluate(_PICK_POINT_JS)
    box = await target.bounding_box(timeout=timeout)
    if box is None:
        raise Exception("element has no layout box")
    if pos is not None:
        x = box["x"] + pos["bl"] + pos["x"]
        y = box["y"] + pos["bt"] + pos["y"]
    else:
        x = box["x"] + box["width"] / 2
        y = box["y"] + box["height"] / 2
    # The cursor trail comes from page.mouse.move: Camoufox's humanize expands
    # each step into a curve. NOT hover(): MEASURED 2026-10-05 on Camoufox 156 /
    # Playwright 1.62 with the cursor parked away from the target, el.hover
    # delivered 0 mousemove events to the page (3 of 3),
    # page.mouse.move(steps=10) 144-180, el.click alone 35-49.
    await page.mouse.move(x, y, steps=random.randint(5, 15))
    _mouse_pos["x"] = x
    _mouse_pos["y"] = y
    await asyncio.sleep(random.uniform(0.05, 0.2))
    kwargs = {"delay": random.uniform(40, 120), "timeout": timeout}
    if pos is not None:
        kwargs["position"] = {"x": pos["x"], "y": pos["y"]}
    # No position: Playwright picks its own point from the element's quads,
    # which handles every shape the sampler above could not.
    await target.click(**kwargs)


async def _stealth_click(page, selector: str, timeout: int = 10000) -> None:
    """Human-like click through a Playwright Locator.

    Camoufox: scroll the target into view, pick a point that hit-tests as the
    target (see _PICK_POINT_JS), move the mouse there (Camoufox's ``humanize``
    draws the trail), dwell 50-200 ms, then ``click`` at that point, which
    waits for the target to be stable and hit-tests it again. A Locator is
    re-resolved on every call, so a node the page re-renders on mousemove is
    found again rather than failing as detached. A form control that cannot be
    clicked itself (a styled checkbox/radio) is clicked through its <label>.

    The earlier path measured the box without scrolling and pressed the mouse
    at those coordinates: a target below the fold got no click, a covered
    target clicked the overlay, and the tool reported success either way.

    A covered target raises :class:`ClickBlocked` on every layer, with no
    fallback. A click Playwright reports as sent (_CLICK_SENT_MARK in its call
    log) is never repeated by a fallback. A failure before that keeps the
    fallback chain (plain click, keyboard, shadow-DOM script click).

    Other layers use plain ``page.click()``.
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
        try:
            await page.click(selector, timeout=timeout)
        except Exception as err:
            blocked = _blocked_click(err, selector)
            if blocked is not None:
                raise blocked from err
            raise
        return

    loc = page.locator(selector).first
    try:
        await loc.wait_for(state="attached", timeout=timeout)
        info = await loc.evaluate(_LABEL_JS)
        label = _label_locator(page, loc, info)
        if not info.get("visible") and label is not None:
            # A hidden <input> (display:none, zero size): only its label can be
            # clicked.
            await _humanized_click(page, label, timeout)
        else:
            await _humanized_click(page, loc, timeout, label=label)
    except Exception as stealth_err:
        blocked = _blocked_click(stealth_err, selector)
        if blocked is not None:
            raise blocked from stealth_err
        if _click_was_sent(stealth_err):
            raise
        logger.warning("Stealth click failed for '%s': %s", selector, stealth_err)
        try:
            await page.click(selector, timeout=timeout)
        except Exception as plain_err:
            blocked = _blocked_click(plain_err, selector)
            if blocked is not None:
                raise blocked from plain_err
            if _click_was_sent(plain_err):
                raise
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


# browser_fill has no overall deadline: a long value legitimately takes a long
# time (about 0.24 s per character measured, so 2,000 characters is about eight
# minutes), and a length-derived deadline cut real fills short and reset the
# page. What it guards against instead is the failure the old deadline existed
# for: a Playwright call into Camoufox that never returns (MEASURED: a
# page.click(timeout=10000) hung 22 minutes; see _TOOL_TIMEOUT_S). So every
# browser call in a fill gets its own stall bound.
#
# 30 s: the longest LEGITIMATE single step is Playwright's own 10 s actionability
# wait inside fill("")/click(); one keystroke (key down, key up) returns in
# milliseconds, and the hold and gap we add between them (at most 0.2 s and 1 s)
# are sleeps outside the bound. 30 s is three times the longest legitimate step,
# so a step still running then is hung, not slow.
_FILL_STALL_S: float = 30.0


class FillStalled(Exception):
    """A browser call inside browser_fill made no progress for _FILL_STALL_S."""


async def _no_stall(awaitable, what: str):
    """Await one browser call of a fill, failing if it stalls."""
    try:
        return await asyncio.wait_for(awaitable, timeout=_FILL_STALL_S)
    except TimeoutError:
        raise FillStalled(
            f"{what} made no progress for {_FILL_STALL_S:.0f}s"
        ) from None


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
        await _no_stall(page.fill(selector, value, timeout=10000), "fill")
        return

    # Clear field reliably (works on React controlled inputs)
    await _no_stall(page.fill(selector, "", timeout=10000), "clearing the field")
    # Click to focus the field
    await _no_stall(page.click(selector, timeout=10000), "focusing the field")
    # Type per-keystroke with hold time + flight time (IKI) jitter.
    # Hold time: log-normal, median ~86ms (CMU Keystroke Dynamics calibration).
    # Flight time: 50-200ms uniform with 5% thinking pauses.
    for i, char in enumerate(value):
        # Hold phase: keydown → hold → keyup
        hold_s = random.lognormvariate(math.log(0.086), 0.35)
        hold_s = max(0.03, min(hold_s, 0.20))  # clamp 30-200ms
        await _no_stall(page.keyboard.down(char), f"keystroke {i + 1} of {len(value)}")
        await asyncio.sleep(hold_s)
        await _no_stall(page.keyboard.up(char), f"keystroke {i + 1} of {len(value)}")
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


def vnc_click_target(
    *,
    win_x: int,
    win_y: int,
    page_left: float,
    page_top: float,
    chrome_h: int,
    dpr: float,
) -> tuple[int, int]:
    """Map a CSS-pixel page coordinate to a PHYSICAL screen coordinate.

    Two different spaces meet here, and mixing them is silent:

    * ``win_x``/``win_y`` come from ``xdotool getwindowgeometry`` — PHYSICAL
      screen pixels, the space VNC input is delivered in.
    * ``page_left``/``page_top`` come from ``getBoundingClientRect()``, and
      ``chrome_h`` from ``outerHeight - innerHeight`` — both CSS pixels.

    Those are the same number only when ``devicePixelRatio == 1``. Anywhere
    else the click lands short of the target by the scale factor, with no
    error — at dpr 1.25 a control 800 CSS-px down the page is clicked 200
    physical pixels high.

    This is pure so the scaled cases can be tested: the display this runs on
    is dpr 1.0, so the bug is dormant here and CANNOT be exercised end to end.
    Note the caller already spoofs window metrics for anti-detection, and
    ``devicePixelRatio`` is itself a common fingerprinting vector, so a dpr
    other than 1.0 is not hypothetical.
    """
    scale = dpr if dpr and dpr > 0 else 1.0
    click_x = win_x + int(page_left * scale)
    click_y = win_y + int((chrome_h + page_top) * scale)
    return click_x, click_y


async def _kill_and_reap(proc) -> None:
    """Kill a subprocess and collect it.

    ``asyncio.wait_for`` cancels the WAIT, never the child. MEASURED: a
    process whose ``communicate()`` was cancelled by ``wait_for`` is still
    running afterwards, with ``returncode is None``. Every timeout path that
    does not do this leaks the process for as long as it chooses to run —
    which, for a probe against a wedged X server, is unbounded.
    """
    try:
        proc.kill()
    except ProcessLookupError:
        return  # already gone; nothing to reap
    with contextlib.suppress(Exception):
        await proc.wait()


async def _read_pointer_position(
    *, timeout_s: float = _POINTER_PROBE_TIMEOUT_S,
) -> tuple[int, int] | None:
    """Read the X pointer's ACTUAL position, or ``None`` if it cannot be read.

    Best-effort by contract: every measurement failure returns ``None`` rather
    than raising, because a click must not be blocked by an unavailable
    measurement. Cancellation is not a measurement failure — the caller is
    gone — so it reaps the child and propagates. It is still LOGGED: an absent
    readback must never read as a clean one. The ``OSError`` catch is
    load-bearing beyond tidiness: a missing ``xdotool`` raises
    ``FileNotFoundError`` here, and the caller's outer handler for that
    exception reports a missing ``vncdo`` and abandons the click.
    """
    try:
        probe = await asyncio.create_subprocess_exec(
            "xdotool", "getmouselocation", "--shell",
            env={**os.environ, "DISPLAY": _VNC_DISPLAY},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError as exc:
        _ts_log.info("VNC: pointer readback unavailable (%s)", type(exc).__name__)
        return None

    try:
        probe_out, _ = await asyncio.wait_for(probe.communicate(), timeout=timeout_s)
    except asyncio.CancelledError:
        # MEASURED: an outer cancellation reaches NEITHER handler below and
        # leaves the child running (returncode None, pid alive). Not an exotic
        # path — browser_navigate cancels this whole call at its 300s ceiling,
        # and a wedged X server is both what holds the probe open and what
        # makes that ceiling get reached.
        await _kill_and_reap(probe)
        raise
    except (TimeoutError, OSError) as exc:
        await _kill_and_reap(probe)
        _ts_log.info("VNC: pointer readback unavailable (%s)", type(exc).__name__)
        return None

    try:
        probe_vals = dict(
            line.split("=", 1)
            for line in probe_out.decode().splitlines()
            if "=" in line
        )
        return int(probe_vals["X"]), int(probe_vals["Y"])
    except (KeyError, ValueError, UnicodeDecodeError) as exc:
        _ts_log.info("VNC: pointer readback unavailable (%s)", type(exc).__name__)
        return None


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
            try:
                xdo_out, _ = await asyncio.wait_for(xdo.communicate(), timeout=3)
            except (asyncio.CancelledError, TimeoutError):
                # Same leak as the pointer probe below: the wait is cancelled,
                # the process is not. Reap it, then re-raise — a timeout falls
                # through to the (0,0) default via the enclosing
                # ``except Exception``, a cancellation propagates straight past
                # it (CancelledError is a BaseException).
                await _kill_and_reap(xdo)
                raise
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
            dpr = 1.0
            _ts_log.info("VNC: chrome_h measurement failed, using default 34")

        click_x, click_y = vnc_click_target(
            win_x=win_x, win_y=win_y,
            page_left=page_coords["left"], page_top=page_coords["top"],
            chrome_h=chrome_h, dpr=dpr,
        )

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

        # WHERE DID THE POINTER ACTUALLY GO? Read it back before clicking.
        #
        # Be precise about what this can and cannot catch, because the two are
        # easy to conflate. It compares the pointer against the coordinate we
        # ASKED FOR, so it detects a DELIVERY failure: a `vncdo move` that
        # reported success and did nothing, a server that clamped the position,
        # a pointer grabbed by something else.
        #
        # It CANNOT detect a wrong coordinate. If the computation above picked
        # the wrong pixel — a coordinate-space mismatch, a window that moved
        # after it was measured, a spoofed dpr — the move delivers the pointer
        # to exactly that wrong pixel and drift is ZERO. Reading the position
        # answers "did the pointer go where we said", never "was where we said
        # correct"; only hit-testing what is under the pointer would answer
        # that, and this path has no element handle to test against.
        #
        # It still earns its place: without it the log records only intent, and
        # a click on the wrong thing is exactly the failure that needs a
        # forensic trail afterwards. The dpr is logged alongside so the
        # coordinate can be recomputed later from the record.
        #
        # Best-effort: a readback failure must not block the click, but it IS
        # reported rather than swallowed, so "unknown" never reads as "fine".
        pointer = await _read_pointer_position()

        if pointer is None:
            _ts_log.info(
                "VNC LANDED: unknown — pointer readback failed; "
                "intended=(%d,%d)", click_x, click_y,
            )
        else:
            actual_x, actual_y = pointer
            drift = abs(actual_x - click_x) + abs(actual_y - click_y)
            _ts_log.info(
                "VNC LANDED: (%d,%d) intended=(%d,%d) drift=%d dpr=%.2f",
                actual_x, actual_y, click_x, click_y, drift, dpr,
            )
            if drift > _POINTER_DRIFT_TOLERANCE_PX:
                # Loud, because the pointer is NOT where we put it — the click
                # about to be sent lands somewhere we never chose. That is a
                # different failure from aiming wrong, and it would otherwise
                # look like an ordinary miss.
                logger.warning(
                    "VNC pointer drift %dpx: intended=(%d,%d) actual=(%d,%d) "
                    "dpr=%.2f — clicking anyway, but the pointer is not where "
                    "it was placed",
                    drift, click_x, click_y, actual_x, actual_y, dpr,
                )

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


async def _detect_interstitial(page, response=None) -> bool:
    """Detect a BLOCKING Cloudflare interstitial (not an embedded widget).

    True only on interstitial evidence: the ``cf-mitigated`` response header
    (Cloudflare's canonical challenge-response marker, language-independent),
    full-page challenge chrome (``#cf-challenge-running`` etc.), or a "just a
    moment" / "ddos-guard" title. A page that merely embeds a Turnstile widget
    on already-loaded content is NOT an interstitial — see _detect_widget.
    """
    # Strongest signal: CF tags challenge responses with cf-mitigated. Best-effort
    # (Camoufox proxying could hide it — the DOM/title checks below are the backstop).
    if response is not None:
        try:
            if response.headers.get("cf-mitigated"):
                return True
        except Exception:
            pass  # header access is best-effort

    # Full-page challenge chrome (fastest DOM signal).
    for selector in _INTERSTITIAL_SELECTORS:
        if await page.query_selector(selector) is not None:
            return True

    # Interstitial title (English / DDoS-Guard; cf-mitigated is the i18n backstop).
    title = (await page.title()).lower()
    return any(ct in title for ct in _CHALLENGE_TITLES)


async def _detect_widget(page) -> bool:
    """Detect an embedded Cloudflare Turnstile widget (non-blocking)."""
    for selector in _WIDGET_SELECTORS:
        if await page.query_selector(selector) is not None:
            return True
    return False


async def _challenge_present(page) -> bool:
    """Secondary "is a challenge still on the page?" check.

    True if a blocking interstitial OR an embedded Turnstile widget is still
    present. Used by the in-ladder "challenge gone?" gates so a still-unsolved
    widget (chrome dismissed, non-English title, no token yet) is NOT mistaken
    for a resolved challenge. The token poll remains the primary success signal;
    this only guards the DOM-based early-exit.
    """
    return await _detect_interstitial(page) or await _detect_widget(page)


async def _wait_for_turnstile(page, response=None, timeout_ms: int = 15000) -> dict | None:
    """Detect and handle a Cloudflare challenge (Turnstile or managed).

    ``response`` is the ``page.goto`` Response when available; its ``cf-mitigated``
    header is the strongest (language-independent) interstitial signal. Only a
    real interstitial runs the full ladder below. A page that merely EMBEDS a
    Turnstile widget (no interstitial) gets a short auto-resolve grace poll and
    returns ``{"status": "embedded"}`` with no VNC/Telegram escalation.

    Phase 1 (auto-resolve): Polls for ``timeout_ms`` for automatic resolution.

    Phase 2 (VNC click): Sends real mouse clicks through VNC during the
    checkbox window.  If VNC fails (connection refused), repairs VNC infra
    and retries.  Up to 3 rounds of: wait-for-checkbox → click → poll.

    Phase 3 (reload + retry): Reloads the page (triggers different challenge
    variant) then repeats VNC click.

    No human escalation — Genesis handles this itself.

    Returns None if neither an interstitial nor a widget is present, else a
    status dict (``resolved`` / ``embedded`` / ``blocked``).
    """
    try:
        # Brief delay for SPA-injected widgets to load
        await asyncio.sleep(0.8)

        _ts_log.info("_wait_for_turnstile called — checking for challenge")
        _ts_log.info("Page URL: %s | Title: %s", page.url, await page.title())

        if not await _detect_interstitial(page, response):
            # No blocking interstitial. A page that merely EMBEDS a Turnstile
            # widget (login/signup forms) is not a challenge: give it only a
            # brief auto-resolve grace poll (in case it is a fast auto-solving
            # variant), then return WITHOUT the VNC ladder or Telegram alert.
            if await _detect_widget(page):
                _ts_log.info("Widget present, no interstitial — short grace poll")
                if await _poll_turnstile_token(page, timeout_ms / 1000, 1.0):
                    _ts_log.info("RESOLVED: auto (embedded widget)")
                    return {"status": "resolved", "method": "auto"}
                _ts_log.info("Embedded widget only — not a blocking challenge")
                return {"status": "embedded", "method": "widget_no_interstitial"}
            _ts_log.info("No interstitial and no widget — no challenge found")
            return None

        _ts_log.info("=== CHALLENGE DETECTED — starting resolution ===")
        _ts_log.info("Page title: %s", await page.title())
        _ts_log.info("Page URL: %s", page.url)
        logger.info("Cloudflare challenge detected — waiting for auto-resolve")

        # Track whether we ever actually observed the challenge in the DOM. A
        # header-only detection (cf-mitigated with no chrome/widget that never
        # renders) must NOT be treated as "gone" by the DOM-based gates below —
        # that would falsely report resolved; it runs the ladder to a blocked
        # result instead. A challenge we DID see and then watched disappear
        # (e.g. a redirect interstitial) is a genuine resolution.
        saw_challenge_dom = await _challenge_present(page)

        async def _challenge_gone() -> bool:
            nonlocal saw_challenge_dom
            if await _challenge_present(page):
                saw_challenge_dom = True
                return False
            return saw_challenge_dom

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
                if await _challenge_gone():
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
            if await _challenge_gone():
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

                if await _challenge_gone():
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
    global _remote_last_url
    # The layer this call uses, not the one it leaves: an abandoned layer's
    # idle clock keeps running.
    _touch(_requested_layer(stealth, remote, tinyfish))
    _ts_log.info("browser_navigate called: url=%s stealth=%s remote=%s tinyfish=%s", url, stealth, remote, tinyfish)

    if tinyfish and remote:
        return {"error": "Cannot use tinyfish and remote simultaneously — pick one."}

    # No timing switch for remote CDP: _human_delay already uses collaborate
    # timing (0.5-2 s) whenever the remote page is active, whatever
    # _collaborate_mode says. Switching the flag on here only made fast timing
    # stick to later Camoufox work (and survive a failed remote connect).

    try:
        page, is_new_tinyfish = await _get_page(
            stealth, remote=remote, cdp_url=cdp_url,
            tinyfish=tinyfish, tinyfish_url=url if tinyfish else None,
        )
    except ConnectionError as e:
        return {"error": str(e)}
    except CamoufoxEngineNotReady as e:
        return {"error": f"Camoufox is not ready: {e}"}
    except BrowserPackagesChanged as e:
        return {"error": str(e)}
    except ImportError as e:
        return {"error": _import_error_message(e)}

    try:
        # Skip goto only when TinyFish session was JUST created with this URL
        # (it already navigated on creation). Subsequent navigations must goto.
        skip_goto = is_new_tinyfish and url
        response = None
        if not skip_goto:
            _ts_log.info("page.goto starting: %s", url)
            response = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            _ts_log.info("page.goto completed — title: %s", await page.title())

        # Challenge detection for local browsers (Camoufox + Chromium).
        # Skip for TinyFish (cloud browser, clean IP) and remote CDP
        # (user watching their own screen — they can handle challenges).
        turnstile_result = None
        if not tinyfish and not remote:
            _ts_log.info("calling _wait_for_turnstile")
            turnstile_result = await _wait_for_turnstile(page, response=response)
            _ts_log.info("_wait_for_turnstile returned: %s", turnstile_result)

        # Track URL for drift detection on remote sessions
        if remote:
            _remote_last_url = page.url

        snapshot = await _snapshot_page(page)

        def _layer_name():
            from genesis.browser.types import BrowserLayer

            if tinyfish:
                return BrowserLayer.TINYFISH.value
            if _is_remote_active():
                return BrowserLayer.REMOTE_CDP.value
            if _is_camoufox_active():
                return BrowserLayer.CAMOUFOX.value
            return BrowserLayer.CHROMIUM.value

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


# A click that opens a new tab or popup is followed: the tools switch to it.
# Playwright emits the page's "popup" event only once the new tab's FIRST
# RESPONSE has arrived, so the wait has to cover that. MEASURED 2026-10-05 on
# the new stack (Camoufox 156 / Playwright 1.62 / patchright 1.62.3), served
# over local HTTP, seconds from click to event:
#   window.open (no noopener): Camoufox 0.06-0.11, patchright 0.016, CDP ~0
#   target=_blank link:        Camoufox 3.1, 3.1, 7.8 (first window of a fresh
#                              profile); patchright 0.03; CDP ~0
#   _blank to a 2 s server:    Camoufox 2.4-3.1, patchright 1.97, CDP 2.01
# A noopener tab in Firefox opens in a new content process, hence the seconds.
# Waiting 10 s after EVERY click would be the price of catching those, so the
# long wait applies only to a click whose target DECLARES a new tab (a link,
# area or form with a non-self target; see _OPENS_NEW_TAB_JS). Every other
# click waits up to 1 s (from the click's return; the 0.3 s navigation settle
# runs inside it), which covers a script's window.open answering within about
# a second. A tab whose first response comes later than its window is not
# followed: the result has no new_page and the tools stay on the original.
# Tab followed by a click -> (the page that opened it, its remote target id,
# its URL at the time). Lets a close walk back to the nearest live ancestor.
_opened_by: dict = {}

_NEW_TAB_WAIT_S: float = 1.0
_NEW_TAB_DECLARED_WAIT_S: float = 10.0

# True when clicking ``e`` opens a new browsing context by declaration: a link
# or area with a target other than the current one, or a submit control whose
# form (or its own formtarget) names one. _parent/_top are the current tab on a
# top-level page.
_OPENS_NEW_TAB_JS = """
(e) => {
  const own = (v) => !!v && !['_self', '_parent', '_top'].includes(v.trim().toLowerCase());
  const link = e.closest('a[href], area[href]');
  if (link && own(link.getAttribute('target'))) return true;
  // Only a SUBMIT control sends its form: a text field, checkbox or
  // type=button inside a target=_blank form opens nothing.
  const ctl = e.closest('button, input');
  const submits = ctl && (ctl.tagName === 'BUTTON'
    ? (ctl.getAttribute('type') || 'submit').toLowerCase() === 'submit'
    : ['submit', 'image'].includes((ctl.type || '').toLowerCase()));
  if (submits && ctl.form) {
    if (own(ctl.getAttribute('formtarget'))) return true;
    if (!ctl.hasAttribute('formtarget') && own(ctl.form.getAttribute('target'))) return true;
  }
  return false;
}
"""


async def _declares_new_tab(page, selector: str) -> bool:
    """Whether the click target declares a new tab (see _OPENS_NEW_TAB_JS).
    Best effort: an unreadable target counts as not declaring one."""
    try:
        return bool(
            await asyncio.wait_for(
                page.locator(selector).first.evaluate(_OPENS_NEW_TAB_JS), timeout=2.0,
            )
        )
    except Exception:
        return False


def _set_layer_page(layer: BrowserLayer, page) -> None:
    global _stealth_page, _page, _remote_page, _tinyfish_page
    if layer is BrowserLayer.CAMOUFOX:
        _stealth_page = page
    elif layer is BrowserLayer.CHROMIUM:
        _page = page
    elif layer is BrowserLayer.REMOTE_CDP:
        _remote_page = page
    else:
        _tinyfish_page = page


async def _follow_new_page(old, new) -> bool:
    """Make ``new`` (a tab or popup a click on ``old`` opened) the page its
    layer drives, and the active page. ``old`` is never closed.

    Remote CDP: the new tab becomes THE Genesis tab (its target id is the one
    a reconnect looks for), because the tools drive one tab per session and
    the drift guard, timing and health checks all key on _remote_page. The
    original tab stays open in the user's Chrome and is no longer driven.

    When ``new`` closes while its layer still drives it (a sign-in popup that
    closes itself), the layer goes back to ``old``. Returns False, following
    nothing, when ``old`` no longer drives any layer.
    """
    global _active_page, _remote_target_id

    layer = _layer_of(old)
    if layer is None:
        return False
    try:
        old_url = old.url
    except Exception:
        old_url = None
    # Who opened it, so a close can walk back to the nearest LIVE ancestor (a
    # popup that opened a popup, then both closed, returns to the original).
    _opened_by[new] = (old, _remote_target_id, old_url)

    def _on_closed(_page) -> None:
        global _active_page, _remote_target_id, _remote_last_url
        if _layer_page(layer) is not new:
            return  # not driven (never followed, or already moved on); keep the chain
        cur, restore = new, None
        while cur in _opened_by:
            prev, prev_target_id, prev_url = _opened_by.pop(cur)
            if _is_page_alive(prev):
                restore = (prev, prev_target_id, prev_url)
                break
            cur = prev
        if restore is None:
            return
        prev, prev_target_id, prev_url = restore
        _set_layer_page(layer, prev)
        if layer is BrowserLayer.REMOTE_CDP:
            _remote_target_id = prev_target_id
            # The restored tab's drift baseline: if it changed while the popup
            # was open, the next action gets the drift advisory.
            _remote_last_url = prev_url
        if _active_page is new:
            _active_page = prev
        logger.info("New tab closed — %s is back on the tab that opened it", layer.value)

    # BEFORE any await: a popup that closes itself during the target-id lookup
    # below must still find its close handler (measured: registered after the
    # await, a close 240-360 ms in was missed and the next reconnect opened a
    # second Genesis tab).
    new.once("close", _on_closed)
    new_target_id = await _cdp_target_id(new) if layer is BrowserLayer.REMOTE_CDP else None
    if new.is_closed() or _layer_page(layer) is not old:
        # Closed while we asked, or the layer moved on: follow nothing.
        _opened_by.pop(new, None)
        return False
    _set_layer_page(layer, new)
    if layer is BrowserLayer.REMOTE_CDP and new_target_id is not None:
        # An unreadable id keeps the old one, never None: a None makes the next
        # reconnect open yet another tab.
        _remote_target_id = new_target_id
    if _active_page is old:
        _active_page = new
    logger.info("Click opened a new tab — %s now drives it: %s", layer.value, new.url)
    return True


async def _impl_browser_click(selector: str) -> dict:
    """Click an element on the current page.

    A new tab or popup the click opens (``target=_blank``, ``window.open``) is
    followed: it becomes the page the tools drive, the result's url and
    snapshot are its own, and ``new_page`` reports it. The original tab stays
    open. Wait windows: _NEW_TAB_WAIT_S / _NEW_TAB_DECLARED_WAIT_S.
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
    new_pages: list = []
    arrived = asyncio.Event()

    def _on_popup(p) -> None:
        new_pages.append(p)
        arrived.set()

    page.on("popup", _on_popup)
    try:
        await _human_delay()
        wait_s = (
            _NEW_TAB_DECLARED_WAIT_S if await _declares_new_tab(page, selector)
            else _NEW_TAB_WAIT_S
        )
        await _stealth_click(page, selector)
        clicked_at = time.monotonic()
        # A click may trigger navigation (form submit, link). The stealth mouse
        # path — unlike page.click() — has no navigation auto-wait, so settle
        # briefly to let a nav commit, then best-effort wait for the new
        # document. Fully swallowed: never break a click that already worked.
        await asyncio.sleep(0.3)
        with contextlib.suppress(Exception):
            await page.wait_for_load_state("domcontentloaded", timeout=3000)
        remaining = wait_s - (time.monotonic() - clicked_at)
        if not new_pages and remaining > 0:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(arrived.wait(), timeout=remaining)
    except Exception as e:
        return {"error": f"Click failed on '{selector}': {e}"}
    finally:
        with contextlib.suppress(Exception):
            page.remove_listener("popup", _on_popup)
    try:
        result: dict = {"clicked": selector}
        if new_pages:
            new = new_pages[0]
            with contextlib.suppress(Exception):
                await new.wait_for_load_state("domcontentloaded", timeout=3000)
            if new.is_closed():
                followed, why = False, "not followed: it closed again before the click returned"
            else:
                async with _browser_lock:
                    followed = await _follow_new_page(page, new)
                why = "not followed: the browser layer changed during the click"
            title = ""
            with contextlib.suppress(Exception):
                title = await new.title()
            result["new_page"] = {"url": new.url, "title": title}
            if followed:
                page = new
            else:
                result["new_page"]["note"] = why
        _update_remote_url()  # Click may cause navigation (form submit, link)
        result["url"] = page.url
        result["snapshot"] = await _snapshot_page(page)
        return result
    except Exception as e:
        return {"error": f"Click failed on '{selector}': {e}"}


async def _impl_browser_fill(selector: str, value: str) -> dict:
    """Fill a form field on the current page."""
    global _active_page
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
    except FillStalled as e:
        # Same recovery as a tool timeout: the browser is hung, so the page is
        # in an unknown state and the next step must be a fresh navigate.
        logger.warning("browser_fill stalled on '%s': %s — resetting active page", selector, e)
        _active_page = None
        return {
            "error": (
                f"Fill stalled on '{selector}': {e}. "
                "Browser state was reset — call browser_navigate to resume."
            )
        }
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
        # Unique per call: consecutive captures must not overwrite one another
        # (a session capturing 8 pages kept only the last one). The timestamp
        # makes a capture SEQUENCE sortable at MICROSECOND resolution — a
        # 1-second stamp ties an 8-capture burst (measured). The full uuid4 hex,
        # not a truncation, keeps collisions negligible across the 7-day ~/tmp
        # retention window. Sibling writer: scripts/browser.py — the stamp
        # FORMAT is asserted on both sides, see the _STAMP regex in each test.
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        screenshot_path = (
            _SCREENSHOT_DIR
            / f"genesis_browser_screenshot_{stamp}_{uuid.uuid4().hex}.png"
        )
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
        url_before = page.url
        snapshot = await _snapshot_page(page)
        # Remote: the caller has now seen the current page, so it becomes the
        # drift baseline. This is what the drift advisory's "call
        # browser_snapshot()" recommendation relies on. Only a REAL snapshot
        # counts (a timed-out or unavailable one showed the caller nothing),
        # and only if the URL held still while it was taken: a navigation in
        # between means the snapshot may describe the old page.
        if not snapshot.startswith(_SNAPSHOT_PLACEHOLDERS) and page.url == url_before:
            _update_remote_url()
        return {"url": page.url, "title": await page.title(), "snapshot": snapshot}
    except Exception as e:
        return {"error": f"Snapshot failed: {e}"}


def _is_patchright_page(page) -> bool:
    return type(page).__module__.startswith("patchright")


async def _evaluate_main_world(page, expression: str):
    """``page.evaluate`` in the page's own JavaScript world.

    patchright evaluates in an isolated world by default (that is how it avoids
    the Runtime.enable leak), where the page's own globals are invisible.
    browser_run_js is documented as the DevTools console, so it asks for the
    main world explicitly on patchright pages. Internal DOM reads elsewhere in
    this module are fine in either world: the DOM is shared.
    """
    if _is_patchright_page(page):
        return await page.evaluate(expression, isolated_context=False)
    return await page.evaluate(expression)


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
        result = await _evaluate_main_world(page, expression)
        _update_remote_url()  # JS may cause navigation
        return {"result": result, "url": page.url}
    except Exception as e:
        return {"error": f"JS execution failed: {e}"}


def _cookie_profiles() -> tuple[tuple[str, Path], ...]:
    """The two local browser profiles the cookie tools cover, labelled."""
    return (("camoufox", _PROFILE_DIR), ("chromium", _CHROMIUM_PROFILE_DIR))


def _live_cookie_context(kind: str):
    """This process's live browser context for a profile, or None.

    While the browser runs, its cookie jar lives in memory and is written back
    to the file, so the live context's cookie API is the only correct way to
    read or change it. Camoufox's persistent context IS the browser object.

    Liveness is the CONTEXT's, not the remembered page's: cookies belong to the
    context, and the user closing one tab leaves the context (and its lock on
    the profile) in place. A context this process holds counts until cleanup
    clears it; a crashed one makes the cookie call fail, which is reported as
    an error, never as an empty result.
    """
    if kind == "camoufox":
        return _stealth_browser if _stealth_cm is not None else None
    return _context


# Bound on one cookie call to a live browser context. Camoufox is the reason:
# a Playwright call into it has hung for 22 minutes (see _TOOL_TIMEOUT_S), and
# these two tools have no tool-level timeout. A cookie read or clear on a
# healthy browser returns in well under a second, so 30 s means a hung browser,
# and the profile then reports an error instead of blocking the tool forever.
_COOKIE_CALL_TIMEOUT_S: float = 30.0


async def _cookie_call(awaitable):
    return await asyncio.wait_for(awaitable, timeout=_COOKIE_CALL_TIMEOUT_S)


def _domain_cookie_pattern(domain: str) -> re.Pattern:
    """Playwright clear_cookies(domain=) filter equal to domain_matches()."""
    return re.compile(r"^\.?(?:.+\.)?" + re.escape(domain) + "$", re.IGNORECASE)


async def _impl_browser_sessions() -> dict:
    """List cookie counts per domain in both local browser profiles.

    Does NOT launch a browser. A profile whose browser runs in this process is
    read through the live context; otherwise the cookie file is read
    read-only. Each entry says which source it used.
    """
    from collections import Counter

    from genesis.browser.profile import BrowserProfileManager

    profiles = []
    for kind, pdir in _cookie_profiles():
        entry: dict = {"browser": kind, "profile_path": str(pdir)}
        try:
            ctx = _live_cookie_context(kind)
            if ctx is not None:
                counts = Counter(
                    (c.get("domain") or "").lstrip(".") for c in await _cookie_call(ctx.cookies())
                )
                entry.update(
                    source="live browser",
                    exists=True,
                    sessions=[
                        {"domain": d, "cookie_count": n} for d, n in sorted(counts.items())
                    ],
                )
            else:
                mgr = BrowserProfileManager(pdir, browser=kind)
                info = mgr.get_info()
                entry.update(
                    source="profile file",
                    exists=info.exists,
                    size_mb=info.size_mb,
                    sessions=[
                        {"domain": s.domain, "cookie_count": s.cookie_count}
                        for s in info.sessions
                    ],
                )
                if info.error:
                    entry["error"] = info.error
                pid = mgr.running_pid()
                if pid is not None:
                    entry["note"] = (
                        f"open in another browser process (pid {pid}); the file "
                        "can lag that browser's live cookies"
                    )
        except Exception as e:
            entry["error"] = f"Failed to read {kind} sessions: {e}"
        profiles.append(entry)
    return {"profiles": profiles}


async def _impl_browser_clear_domain(domain: str) -> dict:
    """Clear the cookies of a domain and its subdomains in both profiles.

    Exact label matching: ``x.com`` clears ``x.com`` and ``*.x.com``, never
    ``netflix.com``. Does NOT launch a browser: a profile whose browser runs in
    this process is cleared through the live context, a closed one in its
    cookie file, and one open in another process is refused (that browser
    would write its cookies back) with the reason in its entry.
    """
    from genesis.browser.profile import (
        BrowserProfileManager,
        ProfileInUse,
        domain_matches,
        normalize_domain,
    )

    try:
        d = normalize_domain(domain)
    except ValueError as e:
        return {"error": str(e)}

    total = 0
    profiles = []
    for kind, pdir in _cookie_profiles():
        entry: dict = {"browser": kind}
        try:
            ctx = _live_cookie_context(kind)
            if ctx is not None:
                n = sum(
                    domain_matches(c.get("domain", ""), d)
                    for c in await _cookie_call(ctx.cookies())
                )
                if n:
                    await _cookie_call(ctx.clear_cookies(domain=_domain_cookie_pattern(d)))
                entry.update(source="live browser", removed=n)
            else:
                n = BrowserProfileManager(pdir, browser=kind).clear_domain(d)
                entry.update(source="profile file", removed=n)
            total += n
        except ProfileInUse as e:
            entry["error"] = str(e)
        except Exception as e:
            entry["error"] = f"Failed to clear {kind} cookies: {e}"
        profiles.append(entry)
    return {"domain": d, "cookies_removed": total, "profiles": profiles}


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
    --remote-debugging-port=9222 and works in a tab of its own (one per
    session, reused on reconnect, never closed; the user's tabs are not
    touched). Real Chrome fingerprint and the user's IP, but not invisible:
    CDP control is itself detectable and clicks are not humanized. Remote
    actions always use collaborate timing (0.5-2 s); the setting is untouched.
    Use when fingerprint scoring blocks Camoufox and the user is available.

    Set tinyfish=True for a cloud-hosted browser via TinyFish Browser API.
    Fresh isolated Chromium on each session. Paid: 1 credit per 4 minutes.
    Use when local browsers fail anti-bot or you need a clean isolated session.

    Each layer is closed after 1 hour without a tool call on it; switching
    layers leaves the previous one open until then (a TinyFish session keeps
    billing until it idles out).

    cdp_url: Override the CDP endpoint. Default: GENESIS_CDP_URL env var.
    Example: browser_navigate("https://jobs.ashbyhq.com/...", remote=True)

    NOTE: If a Cloudflare challenge is detected (Camoufox and Chromium), this
    call works on it before returning (auto-resolve poll, widget clicks, an
    optional solver, VNC clicks, a reload); it does not wait for a person. If
    unresolved, it sends a Telegram alert (when configured) and returns
    turnstile.status == "blocked". This can take most of the 300 s timeout.
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

    The target is scrolled into view and hit-tested before the click. If
    another element covers it (cookie banner, modal, sticky header), the
    click fails with "Click blocked: <covering element> covers '<selector>'":
    dismiss that element and click again.

    A styled checkbox or radio whose <input> is hidden or covered by its own
    decoration is clicked through its <label>.

    Keyboard fallback: if the click fails for another reason before any click
    was sent, the tool tries keyboard activation (focus + Space/Enter). A
    click that may already have been delivered is never repeated.
    For manual keyboard navigation, use browser_press_key with Tab/Space.

    New tab or popup: if the click opens one (target=_blank, window.open),
    the tools switch to it. The result's url and snapshot are the new tab's,
    and "new_page" gives its url and title. The original tab stays open; if
    the new tab later closes itself (a sign-in popup), the tools go back to
    the original. The tab must start loading within 10 s for a link or form
    that declares a new tab, 1 s otherwise, or it is not followed.

    Returns the updated page snapshot after clicking. "clicked" means the
    click was sent; confirm the page changed.
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
    strings take proportionally longer (about 0.24 s per character after a
    pre-delay of up to 15 s). There is no overall deadline: the call fails
    only if one browser step (clearing, focusing, or a single keystroke)
    makes no progress for 30 s, and then the page is reset.
    """
    return await _impl_browser_fill(selector, value)


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

    Saves to a uniquely-named, timestamp-prefixed file under ~/tmp/ (each
    call gets its own file, so consecutive screenshots don't overwrite one
    another and sort chronologically) and returns the path. Always read the
    returned "path" — never reconstruct it. Use the Read tool to view it.
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
    """List logged-in sessions in both local browser profiles.

    Covers Camoufox (~/.genesis/camoufox-profile) and Chromium
    (~/.genesis/browser-profile), one labelled entry each, with cookie counts
    per domain. Never launches a browser: a browser running in this session is
    read live, otherwise the profile's cookie file is read.
    """
    return await _impl_browser_sessions()


@mcp.tool()
async def browser_clear_domain(domain: str) -> dict:
    """Clear cookies for a domain and its subdomains (selective logout).

    Matches whole labels: 'x.com' clears x.com and api.x.com, never
    netflix.com. Covers both local profiles and returns the number removed,
    per profile and in total. Never launches a browser. A profile open in
    another session's browser is refused for that profile (its entry says
    why), because that browser would write its cookies back.
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
    return _impl_browser_collaborate(enable)


def _impl_browser_collaborate(enable: bool = True) -> dict:
    """Set the timing profile explicitly (see browser_collaborate)."""
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
