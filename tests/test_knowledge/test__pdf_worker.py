"""Direct tests for the sync PDF worker.

(Also exercised end-to-end via the real spawn pool in ``test_pdf_extract.py``;
this file covers the pure function directly for the page-count parity contract.)
"""

from __future__ import annotations

from pathlib import Path


def test_extract_pdf_text_sync_counts_only_nonblank_pages(tmp_path: Path):
    import pymupdf

    from genesis.knowledge._pdf_worker import extract_pdf_text_sync

    p = tmp_path / "doc.pdf"
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "Alpha page")
    doc.new_page()  # deliberately blank — must be excluded from the count
    doc.new_page().insert_text((72, 72), "Gamma page")
    doc.save(str(p))
    doc.close()

    full_text, sections, page_count = extract_pdf_text_sync(str(p))

    assert "Alpha page" in full_text
    assert "Gamma page" in full_text
    # Parity with the old inline PDFProcessor: page_count = NON-blank pages only.
    assert page_count == 2
    assert sections == ["Alpha page", "Gamma page"]
