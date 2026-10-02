"""YouTube route for the ``web_fetch`` MCP tool.

A YouTube video URL is fetched through ``YouTubeProcessor`` (yt-dlp: metadata
plus the best caption track; never an audio transcription) instead of the page
HTML, so a session without Bash — the inbox judge — still reads what a video
says. Wired into the MCP tool wrapper only: ``_impl_web_fetch``'s other callers
(the corrective memory search, the dashboard) keep plain page fetches.

If yt-dlp yields nothing usable, a single-URL call falls through to the
ordinary fetch chain (the page's title and description) and reports
``youtube_error``; in a ``urls`` batch the video keeps the batch's page entry.
"""

from __future__ import annotations

import logging
import time

from genesis.knowledge.processors.youtube import YouTubeProcessor, is_youtube_video_url
from genesis.security import ContentSanitizer, ContentSource
from genesis.security.sanitizer import strip_boundary_markers

logger = logging.getLogger(__name__)


def _format(result) -> str:
    meta = result.metadata
    lines = [f"# {meta.get('title') or 'YouTube video'}"]
    for label, key in (
        ("Channel", "channel"),
        ("Duration (s)", "duration"),
        ("Uploaded", "upload_date"),
        ("Language", "language"),
    ):
        if meta.get(key):
            lines.append(f"{label}: {meta[key]}")
    if result.caption:
        cap = result.caption
        lines.append(
            f"Transcript source: {cap['kind']}"
            + (f" captions, {cap['language']}" if cap.get("language") else "")
            + f" (provenance: {cap['provenance']})"
        )
    if not result.tls_verified:
        lines.append("Note: fetched without certificate verification (youtube_fetch tls).")
    # The transcript comes before the description: max_chars clips from the end,
    # and what the video SAYS must survive a small budget (#2568 review).
    lines += ["", "## Transcript", result.transcript or "(no transcript)"]
    if meta.get("description"):
        lines += ["", "## Description", meta["description"]]
    return "\n".join(lines)


async def fetch_youtube(url: str, max_chars: int) -> tuple[dict | None, str | None]:
    """``(result, None)`` on success, ``(None, error)`` when the caller should
    fall back to the ordinary fetch chain; ``(None, None)`` for a non-YouTube URL."""
    url = (url or "").strip()
    if url and not url.startswith(("http://", "https://")):
        url = "https://" + url  # the same normalization _impl_web_fetch applies
    if not is_youtube_video_url(url):
        return None, None
    start = time.monotonic()
    try:
        # Never an audio transcription here: this route is reachable from
        # attacker-authored inbox links, and audio is unbounded work (download,
        # conversion, speech-to-text) that round after round of review had to
        # fence in. Knowledge ingestion keeps it (owner decision, 2026-09-29).
        result = await YouTubeProcessor().fetch(url, audio_fallback=False)
    except Exception as exc:  # never let the route break web_fetch
        logger.warning("YouTube fetch raised for %s", url, exc_info=True)
        return None, f"{type(exc).__name__}: {exc}"
    if not result.transcript:
        error = "; ".join(result.errors) or "no captions"
        logger.info("YouTube fetch produced no transcript for %s: %s", url, error)
        if not result.metadata.get("title"):
            return None, error  # metadata failed too: the page is the best left
        # Metadata came through: return it (it says "(no transcript)") rather
        # than a page fetch that is often only a consent or script shell.
        return _result(url, result, max_chars, start, youtube_error=error, caption=None), None
    return _result(url, result, max_chars, start, caption=result.caption), None


def _result(url: str, result, max_chars: int, start: float, **extra) -> dict:
    """The web_fetch result for a yt-dlp fetch.

    The text is attacker-authorable (captions, description), so it is wrapped in
    the keyed untrusted-content boundary like every WebFetcher page (#2568
    review). It is clipped BEFORE wrapping, so the closing marker survives a
    small ``max_chars``, and forged markers inside the video text are stripped.
    """
    content = _format(result)
    body = strip_boundary_markers(content[:max_chars])
    return {
        "url": url,
        "title": result.metadata.get("title", ""),
        "content": ContentSanitizer().wrap_content(body, ContentSource.WEB_FETCH),
        "backend_used": "yt-dlp",
        "status_code": 200,
        "truncated": len(content) > max_chars,
        "error": None,
        **extra,
        "tls_verified": result.tls_verified,
        "latency_ms": round((time.monotonic() - start) * 1000, 1),
    }
