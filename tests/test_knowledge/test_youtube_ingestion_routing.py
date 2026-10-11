"""Knowledge ingestion routes every YouTube video URL shape to the YouTube processor.

The ingestion registry used a narrower pattern than the processor's own
``is_youtube_video_url``: ``/live/`` and ``/embed/`` URLs fell through to the
generic web processor and lost their transcripts, and a ``watch?v=`` URL with no
video id was sent to the YouTube processor, which then failed. One shared
pattern now covers every shape, always with a video id, and ``can_handle``
re-checks with the predicate. (``m.`` and ``music.`` hosts already matched,
because the registry search is unanchored; they are pinned here too.)
"""

from __future__ import annotations

import pytest

from genesis.knowledge.processors.registry import build_default_registry
from genesis.knowledge.processors.youtube import YouTubeProcessor


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=abc123",
        "https://m.youtube.com/watch?v=abc123",
        "https://music.youtube.com/watch?v=abc123",
        "https://www.youtube.com/live/abc123",
        "https://www.youtube.com/embed/abc123",
        "https://www.youtube.com/shorts/abc123",
        "youtu.be/abc123",
        "https://www.youtube.com/watch?list=PL123&v=abc123",
        "https://www.youtube.com/watch?si=share123&v=abc123",
        "YouTube.com/watch?v=abc123",
    ],
)
def test_every_video_url_shape_goes_to_the_youtube_processor(url):
    processor = build_default_registry().get_processor(url)
    assert isinstance(processor, YouTubeProcessor) and processor.can_handle(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/@somechannel",
        "https://www.youtube.com/live/",
        "https://www.youtube.com/embed/",
        "https://www.youtube.com/watch?v=",
        "https://www.youtube.com/playlist?list=PL123",
    ],
)
def test_a_url_without_a_video_id_is_not_routed_to_the_youtube_processor(url):
    """Codex P2 on #2698: the widened pattern had dropped the required video id."""
    processor = build_default_registry().get_processor(url)
    assert not isinstance(processor, YouTubeProcessor)
    assert not YouTubeProcessor().can_handle(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/?u=youtube.com/watch?v=abc123",
        "https://notyoutube.com/watch?v=abc123",
    ],
)
def test_a_youtube_lookalike_falls_through_to_the_web_processor(url):
    """Review on this PR: the registry routed on the pattern alone, so a URL
    merely containing a YouTube path went to the YouTube processor and failed."""
    from genesis.knowledge.processors.web import WebProcessor

    assert isinstance(build_default_registry().get_processor(url), WebProcessor)
