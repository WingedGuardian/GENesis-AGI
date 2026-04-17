"""Text file processor (.txt, .md, .rst)."""

from __future__ import annotations

import logging
from pathlib import Path

from genesis.knowledge.processors.base import ProcessedContent

logger = logging.getLogger(__name__)

_SUPPORTED_EXTENSIONS = {".txt", ".md", ".rst", ".text", ".markdown"}


class TextProcessor:
    """Process plain text and markdown files."""

    async def process(self, source: str, **kwargs: object) -> ProcessedContent:
        path = Path(source)
        if not path.exists():
            raise FileNotFoundError(f"Text file not found: {source}")

        text = path.read_text(encoding="utf-8", errors="replace")
        return ProcessedContent(
            text=text.strip(),
            metadata={
                "filename": path.name,
                "extension": path.suffix,
                "size_bytes": path.stat().st_size,
            },
            source_type="text",
            source_path=source,
        )

    def can_handle(self, source: str) -> bool:
        return Path(source).suffix.lower() in _SUPPORTED_EXTENSIONS
