"""Medium distribution via Camoufox browser automation."""

from __future__ import annotations

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
        from genesis.mcp.health.browser import _impl_browser_click
        return await _impl_browser_click(selector)

    async def fill(self, selector: str, value: str) -> dict[str, Any]:
        from genesis.mcp.health.browser import _impl_browser_fill
        return await _impl_browser_fill(selector, value)

    async def run_js(self, expression: str) -> dict[str, Any]:
        from genesis.mcp.health.browser import _impl_browser_run_js
        return await _impl_browser_run_js(expression)

    async def snapshot(self) -> dict[str, Any]:
        from genesis.mcp.health.browser import _impl_browser_snapshot
        return await _impl_browser_snapshot()

    async def screenshot(self) -> dict[str, Any]:
        from genesis.mcp.health.browser import _impl_browser_screenshot
        return await _impl_browser_screenshot()

    async def press_key(self, key: str, count: int = 1) -> dict[str, Any]:
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
        # Markdown heading
        match = re.match(r"^#+\s+(.+)$", line)
        if match:
            return match.group(1).strip()
        # First non-empty line as title
        return line[:100]
    return "Untitled"


def _extract_body(content: str) -> str:
    """Extract body text, stripping the title line if it's a heading."""
    lines = content.strip().splitlines()
    if not lines:
        return content
    first = lines[0].strip()
    if re.match(r"^#+\s+", first):
        return "\n".join(lines[1:]).strip()
    return content.strip()


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

        # Logged-in Medium shows "Write" button or user avatar
        logged_in = "Write" in snapshot_text or self._username in snapshot_text
        if not logged_in:
            logger.info("Not logged in to Medium (username: %s)", self._username)
        return logged_in

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

        # Step 1: Check login
        if not await self._check_logged_in():
            return PostResult(
                post_id=None,
                platform="medium",
                url=None,
                status="failed",
                error="Not logged in to Medium. Open VNC and log in manually, then retry.",
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

            # Step 3: Wait for editor and type title
            # Medium's editor uses contenteditable divs, not input fields.
            # The title placeholder is typically the first editable element.
            await self._browser.click('h3[data-contents="true"], [data-testid="post-title"], h3.graf--title')
            await self._browser.run_js(
                f"document.execCommand('insertText', false, {_js_string(title)})"
            )

            # Step 4: Move to body and insert content
            await self._browser.press_key("Enter", count=2)
            # Use insertText for the body — works in contenteditable
            await self._browser.run_js(
                f"document.execCommand('insertText', false, {_js_string(body)})"
            )

            # Step 5: Trigger publish flow
            # Click the "Publish" button (top-right)
            await self._browser.click('button[data-testid="publishButton"], button:has-text("Publish")')

            # Step 6: Handle publish dialog — click final "Publish now"
            await self._browser.click(
                'button[data-testid="confirmPublish"], button:has-text("Publish now")'
            )

            # Step 7: Capture the URL after redirect
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

        try:
            # post_id for Medium is the URL slug or full URL
            url = post_id if post_id.startswith("http") else f"https://medium.com/p/{post_id}"
            result = await self._browser.navigate(url)
            if "error" in result:
                return False

            # Click the "..." menu, then "Delete story"
            await self._browser.click('[aria-label="More actions"], button:has-text("⋯")')
            await self._browser.click('button:has-text("Delete story")')
            # Confirm deletion
            await self._browser.click('button:has-text("Delete"), button:has-text("Confirm")')

            logger.info("Deleted Medium post: %s", post_id)
            return True
        except Exception:
            logger.error("Medium delete failed for %s", post_id, exc_info=True)
            return False


def _js_string(s: str) -> str:
    """Escape a Python string for safe JS string literal embedding."""
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n").replace("\r", "\\r") + "'"
