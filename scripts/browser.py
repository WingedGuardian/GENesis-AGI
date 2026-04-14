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
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

USER_DATA_DIR = Path.home() / ".genesis" / "browser-profile"
DEFAULT_SCREENSHOT = str(Path.home() / "tmp" / "browser_screenshot.png")


def _launch(pw):
    """Launch a persistent Chromium context with container-safe flags."""
    USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    context = pw.chromium.launch_persistent_context(
        user_data_dir=str(USER_DATA_DIR),
        headless=True,
        executable_path="/usr/bin/google-chrome",
        args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"],
    )
    page = context.pages[0] if context.pages else context.new_page()
    return context, page


def cmd_navigate(args):
    with sync_playwright() as pw:
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
    with sync_playwright() as pw:
        context, page = _launch(pw)
        try:
            page.click(args.selector, timeout=10000)
            print(f"Clicked: {args.selector}")
        finally:
            context.close()


def cmd_fill(args):
    with sync_playwright() as pw:
        context, page = _launch(pw)
        try:
            page.fill(args.selector, args.value, timeout=10000)
            print(f"Filled '{args.selector}' with value")
        finally:
            context.close()


def cmd_snapshot(args):
    with sync_playwright() as pw:
        context, page = _launch(pw)
        try:
            snapshot = page.locator("body").aria_snapshot()
            print(snapshot)
        finally:
            context.close()


def cmd_screenshot(args):
    with sync_playwright() as pw:
        context, page = _launch(pw)
        try:
            path = args.path or DEFAULT_SCREENSHOT
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
