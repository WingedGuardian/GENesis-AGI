"""LinkedIn video-post captions for the ``web_fetch`` MCP tool.

A LinkedIn post is fetched as an ordinary page first, which keeps the post text
and comments. When the post has a captioned video, its transcript is appended
as a ``## Video transcript`` section, so a session without Bash (the inbox
judge) also reads what the video says. Most posts have no video; for those,
nothing is added and nothing is reported. The whole result is wrapped in the
untrusted-content boundary by the MCP tool, like every page.
"""

from __future__ import annotations

import logging

from genesis.knowledge.processors.linkedin import LinkedInCaptionProcessor, is_linkedin_post_url

logger = logging.getLogger(__name__)


async def fetch_linkedin_captions(url: str, max_chars: int) -> tuple[str | None, str | None]:
    """``(section, None)`` with the transcript section, ``(None, error)`` when the
    post has a video whose captions could not be read, ``(None, None)`` when there
    is nothing to add (not a post URL, or no video)."""
    url = (url or "").strip()
    if url and not url.startswith(("http://", "https://")):
        url = "https://" + url  # the same normalization _impl_web_fetch applies
    if not is_linkedin_post_url(url):
        return None, None
    try:
        result = await LinkedInCaptionProcessor().fetch(url)
    except Exception as exc:  # never let the caption lookup break web_fetch
        logger.warning("LinkedIn caption fetch raised for %s", url, exc_info=True)
        return None, f"{type(exc).__name__}: {exc}"
    if result.transcript:
        cap = result.caption or {}
        label = f"{cap.get('kind', 'unknown')} captions"
        if cap.get("language"):
            label += f", {cap['language']}"
        label += f", provenance: {cap.get('provenance', 'unknown')}"
        return f"## Video transcript ({label})\n{result.transcript[:max_chars]}", None
    if not result.metadata.get("title"):
        # yt-dlp found no video: an ordinary text post. Nothing to add.
        return None, None
    return None, "; ".join(result.errors) or "the post's video has no captions"
