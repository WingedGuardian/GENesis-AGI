"""Medium distribution via Camoufox browser automation."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Protocol

from genesis.distribution.base import PostResult

logger = logging.getLogger(__name__)


class BrowserClient(Protocol):
    """Browser interaction interface — injectable for testing.

    Production implementation wraps the _impl_* functions from
    genesis.mcp.health.browser. Tests provide a mock.
    """

    async def navigate(self, url: str) -> dict[str, Any]: ...
    async def click(self, selector: str) -> dict[str, Any]: ...
    async def fill(self, selector: str, value: str) -> dict[str, Any]: ...
    async def run_js(self, expression: str) -> dict[str, Any]: ...
    async def snapshot(self) -> dict[str, Any]: ...
    async def screenshot(self) -> dict[str, Any]: ...
    async def press_key(self, key: str, count: int = 1) -> dict[str, Any]: ...


class CamoufoxBrowserClient:
    """Production BrowserClient wrapping browser.py _impl_* functions.

    Starts its own Camoufox instance using the shared persistent profile
    at ~/.genesis/camoufox-profile/. Login cookies survive restarts.
    """

    async def _ensure_browser(self) -> None:
        from genesis.mcp.health.browser import _ensure_browser
        await _ensure_browser()

    async def navigate(self, url: str) -> dict[str, Any]:
        await self._ensure_browser()
        from genesis.mcp.health.browser import _impl_browser_navigate
        return await _impl_browser_navigate(url)

    async def click(self, selector: str) -> dict[str, Any]:
        await self._ensure_browser()
        from genesis.mcp.health.browser import _impl_browser_click
        return await _impl_browser_click(selector)

    async def fill(self, selector: str, value: str) -> dict[str, Any]:
        await self._ensure_browser()
        from genesis.mcp.health.browser import _impl_browser_fill
        return await _impl_browser_fill(selector, value)

    async def run_js(self, expression: str) -> dict[str, Any]:
        await self._ensure_browser()
        from genesis.mcp.health.browser import _impl_browser_run_js
        return await _impl_browser_run_js(expression)

    async def snapshot(self) -> dict[str, Any]:
        await self._ensure_browser()
        from genesis.mcp.health.browser import _impl_browser_snapshot
        return await _impl_browser_snapshot()

    async def screenshot(self) -> dict[str, Any]:
        await self._ensure_browser()
        from genesis.mcp.health.browser import _impl_browser_screenshot
        return await _impl_browser_screenshot()

    async def press_key(self, key: str, count: int = 1) -> dict[str, Any]:
        await self._ensure_browser()
        from genesis.mcp.health.browser import _impl_browser_press_key
        return await _impl_browser_press_key(key, count)


def _extract_title(content: str) -> str:
    """Extract a title from content.

    Uses the first markdown heading if present, otherwise the first line.
    """
    for line in content.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        match = re.match(r"^#+\s+(.+)$", line)
        if match:
            return match.group(1).strip()
        return line[:100]
    return "Untitled"


def _extract_body(content: str) -> str:
    """Extract body text, stripping the title line."""
    lines = content.strip().splitlines()
    if not lines:
        return content
    first = lines[0].strip()
    # Strip markdown heading used as title
    if re.match(r"^#+\s+", first):
        return "\n".join(lines[1:]).strip()
    # Strip plain first line used as title
    return "\n".join(lines[1:]).strip()


# Login detection markers — more specific than just "Write"
_LOGIN_MARKERS = [
    "Write a story",
    "New story",
    "Your stories",
]


class MediumDistributor:
    """Publishes content to Medium via Camoufox browser automation.

    Medium's API is deprecated. This distributor uses a persistent
    Camoufox browser session to navigate Medium's editor, paste content,
    and publish. Login is handled once manually (VNC), cookies persist
    in the Camoufox profile.

    Not wired into the genesis server module — runs from CC sessions
    only (foreground, background, or ego-dispatched direct_session).
    """

    def __init__(
        self,
        *,
        browser: BrowserClient | None = None,
        username: str = "",
    ) -> None:
        self._browser = browser or CamoufoxBrowserClient()
        self._username = username

    @property
    def platform(self) -> str:
        return "medium"

    @property
    def available(self) -> bool:
        # Medium availability is config-only (no network check).
        # Login state is verified at publish time.
        return bool(self._username)

    async def _check_logged_in(self) -> bool:
        """Navigate to Medium and check if we have an active session."""
        result = await self._browser.navigate("https://medium.com/")
        if "error" in result:
            logger.warning("Failed to navigate to Medium: %s", result["error"])
            return False

        snap = await self._browser.snapshot()
        snapshot_text = str(snap.get("snapshot", ""))

        # Check for login-specific markers (not just "Write" which is too generic)
        logged_in = (
            self._username in snapshot_text
            or any(marker in snapshot_text for marker in _LOGIN_MARKERS)
        )
        if not logged_in:
            logger.info("Not logged in to Medium (username: %s)", self._username)
        return logged_in

    async def _attempt_relogin(self) -> bool:
        """Try to re-authenticate via Google OAuth cookies already in profile.

        Medium supports "Sign in with Google". If the Camoufox profile has
        valid Google session cookies, clicking the Google sign-in button
        auto-authenticates without user interaction.

        Returns True if login succeeds, False otherwise.
        """
        logger.info("Attempting automatic Medium re-login via Google OAuth")
        try:
            result = await self._browser.navigate("https://medium.com/m/signin")
            if "error" in result:
                logger.warning("Failed to navigate to sign-in page: %s", result.get("error"))
                return False

            await asyncio.sleep(2)  # Let the sign-in page render
            snap = await self._browser.snapshot()
            snap_text = str(snap.get("snapshot", ""))

            # Look for Google sign-in option
            if "Google" not in snap_text and "google" not in snap_text:
                logger.warning("Google sign-in option not found on Medium login page")
                return False

            # Click the Google sign-in button
            try:
                await self._browser.click('button:has-text("Sign in with Google")')
            except Exception:
                try:
                    await self._browser.click('button:has-text("Continue with Google")')
                except Exception:
                    logger.warning("Could not click Google sign-in button")
                    return False

            # Wait for OAuth redirect (Google cookies should auto-auth)
            await asyncio.sleep(5)

            # Verify login succeeded
            if await self._check_logged_in():
                logger.info("Automatic Medium re-login succeeded")
                return True

            logger.warning("Re-login flow completed but login check still fails")
            return False

        except Exception as exc:
            logger.warning("Medium re-login failed: %s", exc)
            return False

    async def publish(
        self,
        content: str,
        *,
        visibility: str = "PUBLIC",
    ) -> PostResult:
        """Publish a story to Medium.

        Args:
            content: The full content. First markdown heading (or first
                line) becomes the title; remainder is the body.
            visibility: Ignored for Medium (visibility is set in publish
                dialog). Kept for protocol compatibility.

        Returns:
            PostResult with status and any error details.
        """
        if not self.available:
            return PostResult(
                post_id=None,
                platform="medium",
                url=None,
                status="failed",
                error="Medium distributor not configured (missing username)",
            )

        # Step 1: Check login (with automatic re-login attempt)
        if not await self._check_logged_in():
            logger.info("Not logged in — attempting automatic re-login")
            if not await self._attempt_relogin():
                return PostResult(
                    post_id=None,
                    platform="medium",
                    url=None,
                    status="failed",
                    error="Not logged in to Medium and automatic re-login failed. "
                    "Google OAuth cookies may have expired. Manual VNC login required.",
                )

        title = _extract_title(content)
        body = _extract_body(content)

        try:
            # Step 2: Navigate to new story
            result = await self._browser.navigate("https://medium.com/new-story")
            if "error" in result:
                return PostResult(
                    post_id=None, platform="medium", url=None, status="failed",
                    error=f"Failed to open Medium editor: {result['error']}",
                )

            # Step 3: Verify editor loaded
            snap = await self._browser.snapshot()
            snap_text = str(snap.get("snapshot", ""))
            if "Tell your story" not in snap_text and "Title" not in snap_text:
                logger.warning("Editor may not have loaded: %s", snap_text[:200])

            # Step 4: Type title into the editor
            await self._browser.click('h3[data-contents="true"], [data-testid="post-title"], h3.graf--title')
            await self._browser.run_js(
                f"document.execCommand('insertText', false, {json.dumps(title)})"
            )

            # Step 5: Move to body and insert content
            await self._browser.press_key("Enter", count=2)
            await self._browser.run_js(
                f"document.execCommand('insertText', false, {json.dumps(body)})"
            )

            # Step 6: Trigger publish flow
            await self._browser.click('button[data-testid="publishButton"], button:has-text("Publish")')

            # Step 7: Verify publish dialog appeared
            dialog_snap = await self._browser.snapshot()
            dialog_text = str(dialog_snap.get("snapshot", ""))
            if "Publish" not in dialog_text:
                logger.warning("Publish dialog may not have appeared")

            # Step 8: Confirm publish
            await self._browser.click(
                'button[data-testid="confirmPublish"], button:has-text("Publish now")'
            )

            # Step 9: Capture the URL after redirect
            snap = await self._browser.snapshot()
            current_url = snap.get("url", "")

            # Extract post ID from URL (medium.com/@user/title-hash)
            post_id = current_url.split("/")[-1] if current_url else None

            if current_url and "medium.com" in current_url and post_id:
                logger.info("Published to Medium: %s", current_url)
                return PostResult(
                    post_id=post_id,
                    platform="medium",
                    url=current_url,
                    status="published",
                )
            else:
                return PostResult(
                    post_id=None, platform="medium", url=None, status="failed",
                    error="Publish flow completed but could not capture post URL",
                )

        except Exception as exc:
            logger.error("Medium publish error: %s", exc, exc_info=True)
            return PostResult(
                post_id=None, platform="medium", url=None, status="failed",
                error=str(exc)[:500],
            )

    async def delete(self, post_id: str) -> bool:
        """Delete a Medium story by navigating to it and using the menu."""
        if not self.available:
            return False

        # Check login before attempting delete (with re-login attempt)
        if not await self._check_logged_in():
            if not await self._attempt_relogin():
                logger.warning("Cannot delete Medium post — not logged in and re-login failed")
                return False

        try:
            url = post_id if post_id.startswith("http") else f"https://medium.com/p/{post_id}"
            result = await self._browser.navigate(url)
            if "error" in result:
                return False

            await self._browser.click('[aria-label="More actions"], button:has-text("⋯")')
            await self._browser.click('button:has-text("Delete story")')
            await self._browser.click('button:has-text("Delete"), button:has-text("Confirm")')

            logger.info("Deleted Medium post: %s", post_id)
            return True
        except Exception:
            logger.error("Medium delete failed for %s", post_id, exc_info=True)
            return False
