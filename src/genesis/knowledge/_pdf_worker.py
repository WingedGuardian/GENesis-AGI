"""Synchronous PDF text extraction — runs inside a ProcessPoolExecutor worker.

This module is deliberately tiny and free of any ``genesis`` runtime imports so
that a ``spawn``-context worker re-importing it (to unpickle the target function)
pulls in nothing heavy. ``pymupdf`` is imported lazily inside the function, in
the child process, where it is safe to run one document at a time.

See ``genesis.knowledge.pdf_extract`` for the async wrapper and the rationale
(PyMuPDF is not thread-safe and can natively crash/hang on adversarial PDFs).
"""

from __future__ import annotations


def _warmup() -> bool:
    """Trivial task to force a pool worker to spawn (keeps worker imports to
    this tiny module only). See ``pdf_extract._make_pool``."""
    return True


def extract_pdf_text_sync(path: str) -> tuple[str, list[str], int]:
    """Extract text from a PDF file.

    Returns ``(full_text, sections, page_count)``. Semantics are preserved
    exactly from the original inline ``PDFProcessor`` implementation:

    - only pages whose *stripped* text is non-empty are collected;
    - ``page_count`` is the number of NON-BLANK pages (NOT ``len(doc)``);
    - ``sections`` is the list of per-page non-blank texts.
    """
    import pymupdf

    doc = pymupdf.open(path)
    try:
        pages: list[str] = []
        for page in doc:
            text = page.get_text()
            if text.strip():
                pages.append(text.strip())
    finally:
        doc.close()

    full_text = "\n\n".join(pages)
    return full_text, pages, len(pages)
