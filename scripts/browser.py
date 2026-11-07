#!/usr/bin/env python3
"""Lightweight CLI wrapper for Chromium browser automation (patchright when
installed, else Playwright).

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
import importlib.util
import sys
import uuid
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path

USER_DATA_DIR = Path.home() / ".genesis" / "browser-profile"
# The shared payment-card detector, loaded by file path so this CLI still
# imports no genesis modules (it is a stdlib-only file).
_CARD_SHAPES_PATH = (
    Path(__file__).resolve().parent.parent / "src" / "genesis" / "security" / "card_shapes.py"
)


def _installed(dist: str) -> bool:
    try:
        metadata.version(dist)
    except metadata.PackageNotFoundError:
        return False
    return True


def _sync_playwright():
    """``sync_playwright()`` from patchright when it is installed (it hides the
    automation signals plain Playwright leaves), else from Playwright, and the
    package's name. An installed patchright that fails to import raises rather
    than falling back to the detectable Chromium, as the MCP fallback does."""
    if _installed("patchright"):
        from patchright.sync_api import sync_playwright

        return sync_playwright(), "patchright"
    from playwright.sync_api import sync_playwright

    return sync_playwright(), "playwright"


def _prepare(package: str) -> None:
    """What the MCP tools do before a Chromium launch: refuse when the package's
    Chromium is not installed (Playwright's own error says to run `<package>
    install`, a download no session may start), then copy the profile aside when a
    different Chromium is about to open it. Optional: this CLI also runs where
    genesis is not importable, and then it does neither."""
    try:
        from genesis.browser import chromium, profile_backup
    except ImportError:
        print(
            "Note: genesis is not importable here, so the profile is not copied first "
            "and the Chromium install is not checked",
            file=sys.stderr,
        )
        return
    missing = chromium.not_ready(package, headless=True)  # _launch below is headless
    if missing:
        print(f"Chromium is not ready: {missing}", file=sys.stderr)
        sys.exit(1)
    profile_backup.backup_chromium_profile(USER_DATA_DIR, package)  # never raises


def _open():
    manager, package = _sync_playwright()
    _prepare(package)
    return manager


def _default_screenshot_path() -> str:
    """A unique path per capture, so consecutive screenshots don't overwrite.

    A module-level constant cannot do this — it would bind one name for the
    life of the process, which is the bug this replaces. Same scheme as the
    MCP tool's `_impl_browser_screenshot` (genesis/mcp/health/browser.py):
    sortable timestamp + full uuid4 hex. Deliberately duplicated rather than
    imported — this CLI runs standalone; its one genesis import (the launch
    check and the profile copy) is optional.
    """
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return str(Path.home() / "tmp" / f"browser_screenshot_{stamp}_{uuid.uuid4().hex}.png")


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
    with _open() as pw:
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
    with _open() as pw:
        context, page = _launch(pw)
        try:
            page.click(args.selector, timeout=10000)
            print(f"Clicked: {args.selector}")
        finally:
            context.close()


def _contains_card_number(value: str) -> bool:
    """Run the shared detector on ``value``. Raises if it cannot be loaded."""
    spec = importlib.util.spec_from_file_location("_genesis_card_shapes", _CARD_SHAPES_PATH)
    if spec is None or spec.loader is None:
        raise ImportError(str(_CARD_SHAPES_PATH))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.contains_card_number(value)


def cmd_fill(args):
    # A card number is never typed with this tool: refuse before a browser
    # starts, without echoing the value. If the check itself cannot load,
    # refuse too rather than type unchecked.
    try:
        is_card = _contains_card_number(args.value)
    except Exception:  # noqa: BLE001 - any load failure refuses
        print("Refused: the payment-card check could not load "
              f"({_CARD_SHAPES_PATH}), so nothing was filled.", file=sys.stderr)
        sys.exit(2)
    if is_card:
        print("Refused: the value looks like a payment card number. Card numbers "
              "are never typed with this tool.", file=sys.stderr)
        sys.exit(2)
    with _open() as pw:
        context, page = _launch(pw)
        try:
            page.fill(args.selector, args.value, timeout=10000)
            print(f"Filled '{args.selector}' with value")
        finally:
            context.close()


def cmd_snapshot(args):
    with _open() as pw:
        context, page = _launch(pw)
        try:
            snapshot = page.locator("body").aria_snapshot()
            print(snapshot)
        finally:
            context.close()


def cmd_screenshot(args):
    with _open() as pw:
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
        args.func(args)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
