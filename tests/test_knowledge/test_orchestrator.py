"""Tests for knowledge ingestion orchestrator."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from genesis.knowledge.distillation import DistillationPipeline, KnowledgeUnit
from genesis.knowledge.manifest import ManifestManager
from genesis.knowledge.orchestrator import KnowledgeOrchestrator
from genesis.knowledge.processors.registry import ContentProcessorRegistry
from genesis.knowledge.processors.text import TextProcessor


def _make_orchestrator(tmp_path: Path, mock_distill_result: list[KnowledgeUnit] | None = None):
    """Build an orchestrator with a mock distillation pipeline."""
    registry = ContentProcessorRegistry()
    text = TextProcessor()
    registry.register_extensions(text, [".txt", ".md"])

    mock_router = MagicMock()
    distillation = DistillationPipeline(router=mock_router)

    # Mock the distill method
    if mock_distill_result is not None:
        distillation.distill = AsyncMock(return_value=mock_distill_result)

    manifest = ManifestManager(root=tmp_path / "knowledge")

    return KnowledgeOrchestrator(
        registry=registry,
        distillation=distillation,
        manifest=manifest,
    )


async def test_ingest_unknown_source(tmp_path: Path):
    """Unknown source type returns error."""
    orch = _make_orchestrator(tmp_path)
    result = await orch.ingest_source("file.xyz", project_type="test")
    assert result.error is not None
    assert "No processor" in result.error


async def test_ingest_missing_file(tmp_path: Path):
    """Missing file returns processing error."""
    orch = _make_orchestrator(tmp_path)
    result = await orch.ingest_source("/nonexistent/file.txt", project_type="test")
    assert result.error is not None
    assert "Processing failed" in result.error


async def test_ingest_empty_content(tmp_path: Path):
    """Empty file returns quality flag."""
    orch = _make_orchestrator(tmp_path)
    empty_file = tmp_path / "empty.txt"
    empty_file.write_text("")
    result = await orch.ingest_source(str(empty_file), project_type="test")
    assert "empty_content" in result.quality_flags


async def test_ingest_duplicate_detection(tmp_path: Path):
    """Second ingestion of same source returns cached result."""
    units = [KnowledgeUnit(concept="Test", body="Test body", domain="test")]
    orch = _make_orchestrator(tmp_path, mock_distill_result=units)

    # Mock the storage
    with patch("genesis.knowledge.orchestrator.KnowledgeOrchestrator._store_units",
               new_callable=AsyncMock, return_value=["unit-1"]):
        file = tmp_path / "doc.txt"
        file.write_text("Some meaningful content here.")

        r1 = await orch.ingest_source(str(file), project_type="test")
        assert r1.units_created == 1

        r2 = await orch.ingest_source(str(file), project_type="test")
        assert r2.units_created == 0
        assert "duplicate_source" in r2.quality_flags


async def test_ingest_no_units_extracted(tmp_path: Path):
    """Distillation producing zero units flags appropriately."""
    orch = _make_orchestrator(tmp_path, mock_distill_result=[])
    file = tmp_path / "notes.txt"
    file.write_text("Some content that produces nothing meaningful.")
    result = await orch.ingest_source(str(file), project_type="test")
    assert result.units_created == 0
    assert "no_units_extracted" in result.quality_flags


async def test_batch_ingest(tmp_path: Path):
    """Batch ingestion processes all supported files."""
    orch = _make_orchestrator(tmp_path, mock_distill_result=[])

    # Create test files
    (tmp_path / "a.txt").write_text("File A content")
    (tmp_path / "b.md").write_text("File B content")
    (tmp_path / "c.xyz").write_text("Unsupported")

    results = await orch.ingest_batch(str(tmp_path), project_type="test")
    # Should process a.txt and b.md but skip c.xyz
    assert len(results) == 2
