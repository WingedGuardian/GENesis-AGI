"""Tests for knowledge content processors."""

from pathlib import Path

import pytest

from genesis.knowledge.processors.base import ProcessedContent
from genesis.knowledge.processors.pdf import PDFProcessor
from genesis.knowledge.processors.registry import build_default_registry
from genesis.knowledge.processors.text import TextProcessor
from genesis.knowledge.processors.web import WebProcessor
from genesis.knowledge.processors.youtube import YouTubeProcessor

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


# ─── WebProcessor ───────────────────────────────────────────────────────────


def test_web_processor_can_handle():
    processor = WebProcessor()

    assert processor.can_handle("https://example.com")
    assert processor.can_handle("http://example.com")
    assert not processor.can_handle("example.com")
    assert not processor.can_handle("document.pdf")


async def _process_wrapped(monkeypatch, tmp_path, body: str, key_name: str) -> str:
    """Run WebProcessor on a page the fetcher wrapped under a fresh boundary key."""
    from unittest.mock import AsyncMock

    import genesis.security.sanitizer as sanitizer
    from genesis.security.sanitizer import ContentSanitizer, ContentSource
    from genesis.web.fetch import FetchResult, WebFetcher

    monkeypatch.setattr(sanitizer, "_boundary_key", None)
    monkeypatch.setattr("genesis.env.boundary_key_path", lambda: tmp_path / key_name)
    wrapped = ContentSanitizer().wrap_content(body, ContentSource.WEB_FETCH)
    result = FetchResult(url="https://example.com", text=wrapped, title="t", status_code=200)
    monkeypatch.setattr(WebFetcher, "fetch", AsyncMock(return_value=result))
    return (await WebProcessor().process("https://example.com")).text


async def test_web_text_does_not_depend_on_the_boundary_key(monkeypatch, tmp_path):
    """The ingest content hash is taken over this text, so a restored or regenerated
    boundary key must not change it (#2572)."""
    body = "A page long enough to skip the thin-content escalation. " * 20
    first = await _process_wrapped(monkeypatch, tmp_path, body, "key-a")
    second = await _process_wrapped(monkeypatch, tmp_path, body, "key-b")
    assert first == second == body


async def test_marker_shaped_text_inside_a_page_is_kept(monkeypatch, tmp_path):
    body = "<external-content x>\n" + "Page text worth keeping around. " * 20 + "\n</external-content>"
    assert await _process_wrapped(monkeypatch, tmp_path, body, "k") == body


async def test_only_a_complete_fetch_wrapper_is_removed(monkeypatch):
    """A lone opener or closer, or a pair whose ids differ, is page text."""
    from unittest.mock import AsyncMock

    from genesis.web.fetch import FetchResult, WebFetcher

    filler = "Page text worth keeping around. " * 20
    cases = [
        '<external-content source="web_fetch" risk="0.6" id="0123456789abcdef">\n' + filler,
        filler + '\n</external-content id="0123456789abcdef">',
        '<external-content source="web_fetch" risk="0.6" id="0123456789abcdef">\n'
        + filler + '\n</external-content id="fedcba9876543210">',
    ]
    for text in cases:
        result = FetchResult(url="https://example.com", text=text, title="t", status_code=200)
        monkeypatch.setattr(WebFetcher, "fetch", AsyncMock(return_value=result))
        assert (await WebProcessor().process("https://example.com")).text == text


# ─── YouTubeProcessor ───────────────────────────────────────────────────────


def test_youtube_processor_can_handle():
    processor = YouTubeProcessor()

    assert processor.can_handle("https://www.youtube.com/watch?v=abc123")
    assert processor.can_handle("https://youtu.be/abc123")
    assert processor.can_handle("https://youtube.com/shorts/abc123")
    assert not processor.can_handle("https://example.com")
    assert not processor.can_handle("video.mp4")

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
