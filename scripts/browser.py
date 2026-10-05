#!/usr/bin/env python3
"""Lightweight CLI wrapper for Playwright browser automation.

NOTE: For interactive CC sessions, prefer the genesis-health MCP browser_*
tools (lazy-init, persistent session within MCP lifetime). This CLI script
opens and closes the browser on every command, making it slower for
multi-step workflows but useful for one-off tasks and background scripts.

Usage:
    python scripts/browser.py navigate "https://example.com" --screenshot /tmp/page.png
    python scripts/browser.py click "#submit-button"
    python scripts/browser.py fill "#email" "user@example.com"
    python scripts/browser.py snapshot
    python scripts/browser.py screenshot /tmp/capture.png
"""

import argparse
import fcntl
import os
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

USER_DATA_DIR = Path.home() / ".genesis" / "browser-profile"

# The browser-stack lock: the same path as genesis.browser.engine.BROWSER_LOCK_FILE
# (this CLI imports no genesis modules; tests/test_scripts/test_browser_cli_stack_lock.py
# keeps the two equal). A provisioning run holds it EXCLUSIVE; every local browser,
# this one included, holds it SHARED while it may be alive.
STACK_LOCK_FILE = Path.home() / ".genesis" / "locks" / "browser-provision.lock"

# The open descriptor carries the shared hold; closing it, which the kernel does
# when this process exits, is what releases it. Held for the whole run, so the
# browser and its driver are gone before the lock is.
_stack_lock_fd: int | None = None


def _hold_browser_stack() -> None:
    """Hold the browser-stack lock SHARED for the rest of this process.

    Raises RuntimeError when a provisioning run holds it (an upgrade is in
    progress). A lock file that cannot be opened or locked for any other reason
    is reported on stderr and the command proceeds, as the MCP tools do.
    """
    global _stack_lock_fd
    try:
        STACK_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(STACK_LOCK_FILE, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o644)
    except OSError as exc:
        print(f"Warning: could not open the browser-stack lock: {exc}", file=sys.stderr)
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise RuntimeError(
            "the browser stack is being upgraded right now (another process holds the "
            "browser-stack lock exclusively); try again when it finishes"
        ) from None
    except OSError as exc:
        os.close(fd)
        print(f"Warning: could not lock the browser-stack lock: {exc}", file=sys.stderr)
        return
    _stack_lock_fd = fd


def _default_screenshot_path() -> str:
    """A unique path per capture, so consecutive screenshots don't overwrite.

    A module-level constant cannot do this — it would bind one name for the
    life of the process, which is the bug this replaces. Same scheme as the
    MCP tool's `_impl_browser_screenshot` (genesis/mcp/health/browser.py):
    sortable timestamp + full uuid4 hex. Deliberately duplicated rather than
    imported — this CLI stays free of genesis imports so it can run standalone.
    """
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return str(Path.home() / "tmp" / f"browser_screenshot_{stamp}_{uuid.uuid4().hex}.png")


def _sync_playwright():
    """playwright's sync context manager, imported only once the browser-stack
    lock is held (main takes it first), so the import never reads package files
    a provisioning run is halfway through replacing."""
    from playwright.sync_api import sync_playwright

    return sync_playwright()


def _launch(pw):
    """Launch a persistent Chromium context with container-safe flags."""
    USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    context = pw.chromium.launch_persistent_context(
        user_data_dir=str(USER_DATA_DIR),
        headless=True,
        args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"],
    )
    page = context.pages[0] if context.pages else context.new_page()
    return context, page


def cmd_navigate(args):
    with _sync_playwright() as pw:
        context, page = _launch(pw)
        try:
            page.goto(args.url, wait_until="domcontentloaded", timeout=30000)
            print(f"Navigated to {page.url}")
            if args.screenshot:
                page.screenshot(path=args.screenshot)
                print(f"Screenshot saved: {args.screenshot}")
        finally:
            context.close()


def cmd_click(args):
    with _sync_playwright() as pw:
        context, page = _launch(pw)
        try:
            page.click(args.selector, timeout=10000)
            print(f"Clicked: {args.selector}")
        finally:
            context.close()


def cmd_fill(args):
    with _sync_playwright() as pw:
        context, page = _launch(pw)
        try:
            page.fill(args.selector, args.value, timeout=10000)
            print(f"Filled '{args.selector}' with value")
        finally:
            context.close()


def cmd_snapshot(args):
    with _sync_playwright() as pw:
        context, page = _launch(pw)
        try:
            snapshot = page.locator("body").aria_snapshot()
            print(snapshot)
        finally:
            context.close()


def cmd_screenshot(args):
    with _sync_playwright() as pw:
        context, page = _launch(pw)
        try:
            path = args.path or _default_screenshot_path()
            page.screenshot(path=path)
            print(f"Screenshot saved: {path}")
        finally:
            context.close()


def main():
    parser = argparse.ArgumentParser(description="Browser automation CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p_nav = sub.add_parser("navigate", help="Navigate to a URL")
    p_nav.add_argument("url")
    p_nav.add_argument("--screenshot", default=None, help="Save screenshot to path")
    p_nav.set_defaults(func=cmd_navigate)

    p_click = sub.add_parser("click", help="Click an element")
    p_click.add_argument("selector")
    p_click.set_defaults(func=cmd_click)

    p_fill = sub.add_parser("fill", help="Fill a form field")
    p_fill.add_argument("selector")
    p_fill.add_argument("value")
    p_fill.set_defaults(func=cmd_fill)

    p_snap = sub.add_parser("snapshot", help="Print accessibility tree")
    p_snap.set_defaults(func=cmd_snapshot)

    p_ss = sub.add_parser("screenshot", help="Take a screenshot")
    p_ss.add_argument("path", nargs="?", default=None)
    p_ss.set_defaults(func=cmd_screenshot)

    args = parser.parse_args()
    try:
        _hold_browser_stack()
        args.func(args)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
