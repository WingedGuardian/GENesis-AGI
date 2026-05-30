"""Tests for PageIndex tree-based document indexing."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from genesis.knowledge.tree_index import (
    delete_tree_index,
    get_client,
    load_tree_index,
    save_tree_index,
)

# ------------------------------------------------------------------
# get_client() factory
# ------------------------------------------------------------------


def test_get_client_returns_none_without_key():
    """get_client() returns None when API key is not set."""
    with patch.dict("os.environ", {}, clear=True):
        assert get_client() is None


def test_get_client_returns_none_when_sdk_missing():
    """get_client() returns None when pageindex is not importable."""
    with patch.dict("os.environ", {"API_KEY_PAGEINDEX": "test-key"}), patch(
        "genesis.knowledge.tree_index.TreeIndexClient",
        side_effect=ImportError("no module"),
    ):
        assert get_client() is None


# ------------------------------------------------------------------
# save / load / delete round-trip
# ------------------------------------------------------------------


def test_save_load_roundtrip(tmp_path: Path):
    """save_tree_index and load_tree_index round-trip correctly."""
    with patch("genesis.knowledge.tree_index._INDICES_DIR", tmp_path):
        tree = {"result": [{"title": "Intro", "node_id": "0000", "page_index": 1}]}
        path = save_tree_index("/path/to/doc.pdf", "pi-test123", tree)

        assert path.exists()

        loaded = load_tree_index("/path/to/doc.pdf")
        assert loaded is not None
        assert loaded["doc_id"] == "pi-test123"
        assert loaded["tree"] == tree
        assert "indexed_at" in loaded


def test_load_returns_none_for_missing(tmp_path: Path):
    """load_tree_index returns None for non-existent source."""
    with patch("genesis.knowledge.tree_index._INDICES_DIR", tmp_path):
        assert load_tree_index("/nonexistent.pdf") is None


def test_delete_tree_index(tmp_path: Path):
    """delete_tree_index removes the cached file."""
    with patch("genesis.knowledge.tree_index._INDICES_DIR", tmp_path):
        save_tree_index("/path/to/doc.pdf", "pi-test", {})
        assert load_tree_index("/path/to/doc.pdf") is not None

        delete_tree_index("/path/to/doc.pdf")
        assert load_tree_index("/path/to/doc.pdf") is None


# ------------------------------------------------------------------
# Orchestrator integration
# ------------------------------------------------------------------


async def test_orchestrator_skips_tree_index_when_no_client(tmp_path: Path):
    """Orchestrator skips tree indexing when tree_index_client is None."""
    from genesis.knowledge.distillation import DistillationPipeline
    from genesis.knowledge.manifest import ManifestManager
    from genesis.knowledge.orchestrator import KnowledgeOrchestrator
    from genesis.knowledge.processors.registry import ContentProcessorRegistry
    from genesis.knowledge.processors.text import TextProcessor

    registry = ContentProcessorRegistry()
    registry.register_extensions(TextProcessor(), [".txt"])
    distillation = DistillationPipeline(router=MagicMock())
    distillation.distill = AsyncMock(return_value=[])
    manifest = ManifestManager(root=tmp_path / "knowledge")

    orch = KnowledgeOrchestrator(
        registry=registry,
        distillation=distillation,
        manifest=manifest,
        tree_index_client=None,  # explicitly no client
    )

    file = tmp_path / "test.txt"
    file.write_text("Some content.")
    result = await orch.ingest_source(str(file), project_type="test")

    assert result.tree_index_doc_id is None
    assert "tree_index_failed" not in result.quality_flags


async def test_orchestrator_skips_tree_index_below_threshold(tmp_path: Path):
    """Orchestrator skips tree indexing for PDFs below page threshold."""
    from genesis.knowledge.distillation import DistillationPipeline
    from genesis.knowledge.manifest import ManifestManager
    from genesis.knowledge.orchestrator import KnowledgeOrchestrator
    from genesis.knowledge.processors.base import ProcessedContent
    from genesis.knowledge.processors.registry import ContentProcessorRegistry

    mock_processor = MagicMock()
    mock_processor.process = AsyncMock(return_value=ProcessedContent(
        text="short doc",
        metadata={"page_count": 5},  # below threshold
        source_type="pdf",
        source_path="/fake.pdf",
    ))

    registry = ContentProcessorRegistry()
    registry.get_processor = MagicMock(return_value=mock_processor)

    distillation = DistillationPipeline(router=MagicMock())
    distillation.distill = AsyncMock(return_value=[])
    manifest = ManifestManager(root=tmp_path / "knowledge")

    mock_tree_client = MagicMock()

    orch = KnowledgeOrchestrator(
        registry=registry,
        distillation=distillation,
        manifest=manifest,
        tree_index_client=mock_tree_client,
        tree_index_threshold=25,
    )

    result = await orch.ingest_source("/fake.pdf", project_type="test")

    # Tree client should never have been called
    mock_tree_client.upload_document.assert_not_called()
    assert result.tree_index_doc_id is None


async def test_orchestrator_continues_on_tree_index_failure(tmp_path: Path):
    """Orchestrator continues normally when tree indexing fails."""
    from genesis.knowledge.distillation import DistillationPipeline
    from genesis.knowledge.manifest import ManifestManager
    from genesis.knowledge.orchestrator import KnowledgeOrchestrator
    from genesis.knowledge.processors.base import ProcessedContent
    from genesis.knowledge.processors.registry import ContentProcessorRegistry

    mock_processor = MagicMock()
    mock_processor.process = AsyncMock(return_value=ProcessedContent(
        text="long doc " * 1000,
        metadata={"page_count": 50},  # above threshold
        source_type="pdf",
        source_path=str(tmp_path / "big.pdf"),
    ))

    registry = ContentProcessorRegistry()
    registry.get_processor = MagicMock(return_value=mock_processor)

    distillation = DistillationPipeline(router=MagicMock())
    distillation.distill = AsyncMock(return_value=[])
    manifest = ManifestManager(root=tmp_path / "knowledge")

    # Tree client that fails
    mock_tree_client = MagicMock()
    mock_tree_client.upload_document = AsyncMock(
        side_effect=RuntimeError("PageIndex down")
    )

    # Create the fake file so Path(source).exists() is True
    big_pdf = tmp_path / "big.pdf"
    big_pdf.write_bytes(b"%PDF-1.4 fake")

    orch = KnowledgeOrchestrator(
        registry=registry,
        distillation=distillation,
        manifest=manifest,
        tree_index_client=mock_tree_client,
        tree_index_threshold=25,
    )

    result = await orch.ingest_source(str(big_pdf), project_type="test")

    # Should complete without error, with tree_index_failed flag
    assert result.error is None
    assert "tree_index_failed" in result.quality_flags
    assert result.tree_index_doc_id is None
