"""Content processor registry — maps file types and URL patterns to processors."""

from __future__ import annotations

import logging
import re
from pathlib import Path

from genesis.knowledge.processors.base import ContentProcessor

logger = logging.getLogger(__name__)


class ContentProcessorRegistry:
    """Registry that routes sources to the appropriate content processor."""

    def __init__(self) -> None:
        self._extension_map: dict[str, ContentProcessor] = {}
        self._url_patterns: list[tuple[re.Pattern[str], ContentProcessor]] = []

    def register_extensions(
        self, processor: ContentProcessor, extensions: list[str]
    ) -> None:
        """Register a processor for file extensions (e.g., ['.pdf', '.PDF'])."""
        for ext in extensions:
            self._extension_map[ext.lower()] = processor

    def register_url_pattern(
        self, processor: ContentProcessor, pattern: str
    ) -> None:
        """Register a processor for URLs matching a regex pattern."""
        self._url_patterns.append((re.compile(pattern), processor))

    def get_processor(self, source: str) -> ContentProcessor | None:
        """Find the appropriate processor for a source path or URL."""
        # Check URL patterns first (URLs may also have extensions)
        for pattern, processor in self._url_patterns:
            if pattern.search(source):
                return processor

        # Fall back to extension matching for file paths
        ext = Path(source).suffix.lower()
        if ext in self._extension_map:
            return self._extension_map[ext]

        return None

    def supported_extensions(self) -> list[str]:
        """List all registered file extensions."""
        return sorted(self._extension_map.keys())


def build_default_registry() -> ContentProcessorRegistry:
    """Create a registry with all built-in processors."""
    from genesis.knowledge.processors.audio import AudioProcessor
    from genesis.knowledge.processors.image import ImageProcessor
    from genesis.knowledge.processors.pdf import PDFProcessor
    from genesis.knowledge.processors.text import TextProcessor
    from genesis.knowledge.processors.video import VideoProcessor
    from genesis.knowledge.processors.web import WebProcessor
    from genesis.knowledge.processors.youtube import YouTubeProcessor

    registry = ContentProcessorRegistry()

    # Text files
    text = TextProcessor()
    registry.register_extensions(text, [".txt", ".md", ".rst", ".text", ".markdown"])

    # PDF
    pdf = PDFProcessor()
    registry.register_extensions(pdf, [".pdf"])

    # Audio
    audio = AudioProcessor()
    registry.register_extensions(audio, [".mp3", ".wav", ".ogg", ".flac", ".m4a", ".opus"])

    # Video
    video = VideoProcessor()
    registry.register_extensions(video, [".mp4", ".webm", ".mkv", ".avi", ".mov"])

    # Image (stub)
    image = ImageProcessor()
    registry.register_extensions(image, [".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff"])

    # YouTube URLs (must be registered before generic web)
    youtube = YouTubeProcessor()
    registry.register_url_pattern(
        youtube,
        r"(?:https?://)?(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/shorts/)",
    )

    # Generic web URLs (catch-all for http/https)
    web = WebProcessor()
    registry.register_url_pattern(web, r"^https?://")

    return registry
