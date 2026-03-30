"""Tests for source_pipeline provenance tagging on memory store/retrieval."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from genesis.memory.store import MemoryStore
from genesis.memory.types import RetrievalResult


@pytest.fixture()
def embedding_provider():
    ep = MagicMock()
    ep.embed = AsyncMock(return_value=[0.1] * 1024)
    ep.enrich = MagicMock(return_value="episodic: test content")
    return ep


@pytest.fixture()
def qdrant():
    return MagicMock()


@pytest.fixture()
def db():
    return AsyncMock()


@pytest.fixture()
def store(embedding_provider, qdrant, db):
    return MemoryStore(
        embedding_provider=embedding_provider,
        qdrant_client=qdrant,
        db=db,
    )


@pytest.mark.asyncio()
async def test_store_with_source_pipeline_includes_in_payload(store):
    """source_pipeline should appear in the Qdrant upsert payload."""
    with patch("genesis.memory.store.upsert_point") as mock_upsert, \
         patch("genesis.memory.store.memory_crud") as mock_mem:
        mock_mem.find_exact_duplicate = AsyncMock(return_value=None)
        mock_mem.upsert = AsyncMock(return_value="id")
        await store.store("test content", "src", source_pipeline="recon")

    payload = mock_upsert.call_args.kwargs["payload"]
    assert payload["source_pipeline"] == "recon"


@pytest.mark.asyncio()
async def test_store_without_source_pipeline_omits_from_payload(store):
    """When source_pipeline is None, key should be absent (sparse storage)."""
    with patch("genesis.memory.store.upsert_point") as mock_upsert, \
         patch("genesis.memory.store.memory_crud") as mock_mem:
        mock_mem.find_exact_duplicate = AsyncMock(return_value=None)
        mock_mem.upsert = AsyncMock(return_value="id")
        await store.store("test content", "src")

    payload = mock_upsert.call_args.kwargs["payload"]
    assert "source_pipeline" not in payload


@pytest.mark.asyncio()
async def test_store_source_pipeline_various_values(store):
    """All expected source_pipeline values should work."""
    for pipeline in ("conversation", "reflection", "recon", "harvest", "mail"):
        with patch("genesis.memory.store.upsert_point") as mock_upsert, \
             patch("genesis.memory.store.memory_crud") as mock_mem:
            mock_mem.find_exact_duplicate = AsyncMock(return_value=None)
            mock_mem.upsert = AsyncMock(return_value="id")
            await store.store("test content", "src", source_pipeline=pipeline)

        payload = mock_upsert.call_args.kwargs["payload"]
        assert payload["source_pipeline"] == pipeline


def test_retrieval_result_includes_source_pipeline():
    """RetrievalResult dataclass should accept and expose source_pipeline."""
    result = RetrievalResult(
        memory_id="test-id",
        content="test",
        source="src",
        memory_type="episodic",
        score=0.9,
        vector_rank=1,
        fts_rank=None,
        activation_score=0.5,
        payload={"source_pipeline": "reflection"},
        source_pipeline="reflection",
    )
    assert result.source_pipeline == "reflection"


def test_retrieval_result_source_pipeline_defaults_none():
    """RetrievalResult.source_pipeline should default to None."""
    result = RetrievalResult(
        memory_id="test-id",
        content="test",
        source="src",
        memory_type="episodic",
        score=0.9,
        vector_rank=1,
        fts_rank=None,
        activation_score=0.5,
        payload={},
    )
    assert result.source_pipeline is None


def test_extraction_kwargs_include_source_pipeline():
    """extractions_to_store_kwargs should include source_pipeline='harvest'."""
    from genesis.memory.extraction import Extraction, extractions_to_store_kwargs

    extraction = Extraction(
        content="Test entity",
        extraction_type="entity",
        confidence=0.8,
        entities=["Test"],
    )
    kwargs = extractions_to_store_kwargs(extraction)
    assert kwargs["source_pipeline"] == "harvest"
