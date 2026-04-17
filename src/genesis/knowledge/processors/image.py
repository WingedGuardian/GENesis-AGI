"""Image processor — stub for future vision model integration."""

from __future__ import annotations

import logging
from pathlib import Path

from genesis.knowledge.processors.base import ProcessedContent

logger = logging.getLogger(__name__)

_SUPPORTED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff"}


class ImageProcessor:
    """Extract text from images. Currently a stub — returns OCR-like placeholder."""

    async def process(self, source: str, **kwargs: object) -> ProcessedContent:
        path = Path(source)
        if not path.exists():
            raise FileNotFoundError(f"Image file not found: {source}")

        # GROUNDWORK(vision-ocr): Full implementation needs vision model routing.
        # For now, return a metadata-only result that flags the image for manual
        # review or future processing when vision models are wired.
        raise NotImplementedError(
            f"Image processing not yet implemented for {source}. "
            "Vision model integration required."
        )

    def can_handle(self, source: str) -> bool:
        return Path(source).suffix.lower() in _SUPPORTED_EXTENSIONS
