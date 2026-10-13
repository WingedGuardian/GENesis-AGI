"""LinkedIn video-post captions for the ``web_fetch`` MCP tool.

A LinkedIn post is fetched as an ordinary page first, which keeps the post text
and comments. When the post has a captioned video, its transcript comes back
in a separate ``video_transcript`` field beside the page, so a session
without Bash (the inbox judge) also reads what the video says. Most posts have no video; for those,
nothing is added and nothing is reported. The whole result is wrapped in the
untrusted-content boundary by the MCP tool, like every page.
"""

from __future__ import annotations

import logging

from genesis.knowledge.processors.linkedin import LinkedInCaptionProcessor, is_linkedin_post_url

logger = logging.getLogger(__name__)


# yt-dlp's LinkedIn extractor raises this when the post page has no <video>
# tag (extractor/linkedin.py, _search_regex(..., 'video')). It is the one
# positive sign of an ordinary text post; any other failure is reported.
_NO_VIDEO = "unable to extract video"


async def fetch_linkedin_captions(url: str, max_chars: int) -> dict:
    """Fields to add beside a LinkedIn post's page result, or ``{}``.

    ``{}`` for a non-post URL or a text post (no video). A captioned video gives
    ``video_transcript`` (clipped to ``max_chars``, ``video_transcript_truncated``
    when clipped), ``video_caption`` and ``video_tls_verified``. A video whose
    captions could not be read, or a lookup that failed, gives ``video_error``.
    The caller passes a URL that already has its scheme.
    """
    url = (url or "").strip()
    if not is_linkedin_post_url(url):
        return {}
    try:
        result = await LinkedInCaptionProcessor().fetch(url)
    except Exception as exc:  # never let the caption lookup break web_fetch
        logger.warning("LinkedIn caption fetch raised for %s", url, exc_info=True)
        return {"video_error": f"{type(exc).__name__}: {exc}"}
    if result.transcript:
        out = {
            "video_transcript": result.transcript[:max_chars],
            "video_caption": result.caption,
            "video_tls_verified": result.tls_verified,
        }
        if len(result.transcript) > max_chars:
            out["video_transcript_truncated"] = True
        return out
    errors = "; ".join(result.errors)
    if not result.metadata.get("title") and _NO_VIDEO in errors.lower():
        return {}  # an ordinary text post: nothing to add
    return {"video_error": errors or "the post's video has no captions"}
