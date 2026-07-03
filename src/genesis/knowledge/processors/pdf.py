"""PDF processor using PyMuPDF."""

from __future__ import annotations

import logging
from pathlib import Path

from genesis.knowledge.processors.base import ProcessedContent

logger = logging.getLogger(__name__)


class PDFProcessor:
    """Extract text from PDF files using PyMuPDF."""

    async def process(self, source: str, **kwargs: object) -> ProcessedContent:
        from genesis.knowledge.pdf_extract import extract_pdf_text

        path = Path(source)
        if not path.exists():
            raise FileNotFoundError(f"PDF file not found: {source}")

        # PyMuPDF is not thread-safe and can natively crash/hang on adversarial
        # PDFs; extraction runs in a crash/hang-isolated process pool off the
        # event loop. Raises PDFExtractionError on crash/timeout (caller degrades).
        full_text, sections, page_count = await extract_pdf_text(str(path))

        return ProcessedContent(
            text=full_text,
            metadata={
                "filename": path.name,
                "page_count": page_count,
                "size_bytes": path.stat().st_size,
            },
            source_type="pdf",
            source_path=source,
            sections=sections if len(sections) > 1 else None,
        )

    def can_handle(self, source: str) -> bool:
        return Path(source).suffix.lower() == ".pdf"
