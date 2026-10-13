"""LinkedIn video posts: the caption track, through yt-dlp.

A LinkedIn post's page fetch returns the post text and comments but never what
its video says. yt-dlp's LinkedIn extractor reads the post's caption track
without an account, so ``web_fetch`` appends that transcript to the page it
already fetched. Captions only: audio is never downloaded or transcribed on
this route.

Everything else is ``YouTubeProcessor``'s machinery: one fixed argv
(``--ignore-config``, no cookies, one extractor, ``--`` before the URL), the
shared yt-dlp process limit, cancellation that kills the process group, and the
certificate lever. Caption tracks are accepted only from LinkedIn's media hosts.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from genesis.knowledge.processors.youtube import YouTubeFetch, YouTubeProcessor

_LINKEDIN_HOSTS = frozenset({"linkedin.com", "www.linkedin.com"})
# /posts/<author>_<slug>-activity-<id>-<suffix> and /feed/update/urn:li:activity:<id>
_ACTIVITY_PATH = re.compile(r"/feed/update/urn:li:(?:activity|ugcPost|share):\d+/?")


def is_linkedin_post_url(url: str) -> bool:
    """True for a single LinkedIn post URL (not a profile, company or feed page)."""
    try:
        parts = urlsplit(url.strip())
        host = (parts.hostname or "").lower()
    except ValueError:
        return False
    if parts.scheme not in ("http", "https") or host not in _LINKEDIN_HOSTS:
        return False
    if parts.path.startswith("/posts/"):
        return len(parts.path.strip("/").split("/")) == 2 and bool(parts.path.split("/")[2])
    return bool(_ACTIVITY_PATH.fullmatch(parts.path))


class LinkedInCaptionProcessor(YouTubeProcessor):
    """Caption track of a LinkedIn video post; never audio."""

    site = "LinkedIn"
    extractor = "linkedin"
    caption_hosts = ("licdn.com", (".licdn.com",))
    is_video_url = staticmethod(is_linkedin_post_url)

    async def fetch(self, url: str, *, audio_fallback: bool = False) -> YouTubeFetch:
        return await super().fetch(url, audio_fallback=False)
