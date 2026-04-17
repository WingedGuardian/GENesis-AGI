"""Tests for knowledge content processors."""

from pathlib import Path

import pytest

from genesis.knowledge.processors.base import ProcessedContent
from genesis.knowledge.processors.pdf import PDFProcessor
from genesis.knowledge.processors.registry import build_default_registry
from genesis.knowledge.processors.text import TextProcessor

# ─── TextProcessor ──────────────────────────────────────────────────────────


async def test_text_processor_reads_file(tmp_path: Path):
    p = tmp_path / "notes.md"
    p.write_text("# My Notes\n\nSome content here.")

    processor = TextProcessor()
    result = await processor.process(str(p))

    assert isinstance(result, ProcessedContent)
    assert "My Notes" in result.text
    assert result.source_type == "text"
    assert result.metadata["extension"] == ".md"


async def test_text_processor_missing_file(tmp_path: Path):
    processor = TextProcessor()
    with pytest.raises(FileNotFoundError):
        await processor.process(str(tmp_path / "nonexistent.txt"))


async def test_text_processor_can_handle():
    processor = TextProcessor()
    assert processor.can_handle("notes.md")
    assert processor.can_handle("README.txt")
    assert processor.can_handle("doc.rst")
    assert not processor.can_handle("image.png")
    assert not processor.can_handle("data.pdf")


# ─── PDFProcessor ───────────────────────────────────────────────────────────


async def test_pdf_processor_reads_file(tmp_path: Path):
    """Create a minimal PDF and extract text from it."""
    import pymupdf

    pdf_path = tmp_path / "test.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Hello from PDF")
    doc.save(str(pdf_path))
    doc.close()

    processor = PDFProcessor()
    result = await processor.process(str(pdf_path))

    assert isinstance(result, ProcessedContent)
    assert "Hello from PDF" in result.text
    assert result.source_type == "pdf"
    assert result.metadata["page_count"] == 1


async def test_pdf_processor_can_handle():
    processor = PDFProcessor()
    assert processor.can_handle("document.pdf")
    assert processor.can_handle("DOCUMENT.PDF")
    assert not processor.can_handle("document.txt")


# ─── Registry ───────────────────────────────────────────────────────────────


async def test_registry_routes_by_extension():
    registry = build_default_registry()

    assert registry.get_processor("notes.txt") is not None
    assert registry.get_processor("doc.pdf") is not None
    assert registry.get_processor("song.mp3") is not None
    assert registry.get_processor("movie.mp4") is not None


async def test_registry_routes_youtube_before_generic_web():
    registry = build_default_registry()

    yt_processor = registry.get_processor("https://www.youtube.com/watch?v=abc123")
    web_processor = registry.get_processor("https://example.com/article")

    # YouTube should get a different processor than generic web
    assert yt_processor is not None
    assert web_processor is not None
    assert type(yt_processor).__name__ == "YouTubeProcessor"
    assert type(web_processor).__name__ == "WebProcessor"


async def test_registry_returns_none_for_unknown():
    registry = build_default_registry()
    assert registry.get_processor("data.xyz") is None


async def test_registry_supported_extensions():
    registry = build_default_registry()
    exts = registry.supported_extensions()
    assert ".pdf" in exts
    assert ".txt" in exts
    assert ".mp3" in exts


# ─── Manifest ───────────────────────────────────────────────────────────────


async def test_manifest_basic_operations(tmp_path: Path):
    from genesis.knowledge.manifest import ManifestManager

    mgr = ManifestManager(root=tmp_path)

    # Initially empty
    assert not mgr.has_source("/path/to/file.pdf")
    assert mgr.list_sources() == []

    # Save extracted text
    extracted = mgr.save_extracted_text("/path/to/file.pdf", "extracted content", "pdf")
    assert extracted.exists()
    assert extracted.read_text() == "extracted content"

    # Register source
    mgr.add_source(
        "/path/to/file.pdf",
        source_type="pdf",
        extracted_path=extracted,
        unit_ids=["unit-1"],
    )
    assert mgr.has_source("/path/to/file.pdf")
    assert mgr.get_units_for_source("/path/to/file.pdf") == ["unit-1"]

    # Add more unit IDs
    mgr.add_unit_ids("/path/to/file.pdf", ["unit-2", "unit-3"])
    assert mgr.get_units_for_source("/path/to/file.pdf") == ["unit-1", "unit-2", "unit-3"]

    # List sources
    sources = mgr.list_sources()
    assert len(sources) == 1
    assert sources[0]["source_type"] == "pdf"
