"""PDF processor using PyMuPDF."""

from __future__ import annotations

import logging
from pathlib import Path

from genesis.knowledge.processors.base import ProcessedContent

logger = logging.getLogger(__name__)


class PDFProcessor:
    """Extract text from PDF files using PyMuPDF."""

    async def process(self, source: str, **kwargs: object) -> ProcessedContent:
        import pymupdf

        path = Path(source)
        if not path.exists():
            raise FileNotFoundError(f"PDF file not found: {source}")

        doc = pymupdf.open(str(path))
        pages: list[str] = []
        sections: list[str] = []

        for page in doc:
            text = page.get_text()
            if text.strip():
                pages.append(text.strip())
                sections.append(text.strip())

        full_text = "\n\n".join(pages)
        doc.close()

        return ProcessedContent(
            text=full_text,
            metadata={
                "filename": path.name,
                "page_count": len(pages),
                "size_bytes": path.stat().st_size,
            },
            source_type="pdf",
            source_path=source,
            sections=sections if len(sections) > 1 else None,
        )

    def can_handle(self, source: str) -> bool:
        return Path(source).suffix.lower() == ".pdf"
