"""Tests for HybridRetriever embedding fallback (FTS5-only mode)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from genesis.memory.embeddings import EmbeddingUnavailableError
from genesis.memory.retrieval import HybridRetriever


def _make_fts_row(mid: str, rank: float) -> dict:
    return {
        "memory_id": mid,
        "content": f"fts content for {mid}",
        "source_type": "memory",
        "collection": "episodic_memory",
        "rank": rank,
    }


def _build_retriever(*, embed_side_effect=None):
    embed_provider = MagicMock()
    if embed_side_effect:
        embed_provider.embed = AsyncMock(side_effect=embed_side_effect)
    else:
        embed_provider.embed = AsyncMock(return_value=[0.1] * 1024)
    qdrant_client = MagicMock()
    db = MagicMock(spec_set=["execute", "commit"])
    return HybridRetriever(
        embedding_provider=embed_provider,
        qdrant_client=qdrant_client,
        db=db,
    ), embed_provider, qdrant_client, db


@pytest.mark.asyncio
@patch("genesis.memory.retrieval.memory_links")
@patch("genesis.memory.retrieval.memory_crud")
@patch("genesis.memory.retrieval.qdrant_ops")
async def test_recall_fts_only_on_embedding_failure(mock_qdrant, mock_crud, mock_links):
    """When embedding fails, retrieval falls back to FTS5-only and returns results."""
    retriever, _, _, _ = _build_retriever(
        embed_side_effect=EmbeddingUnavailableError("all down")
    )

    mock_crud.search_ranked = AsyncMock(return_value=[
        _make_fts_row("mem-1", -5.0),
        _make_fts_row("mem-2", -3.0),
    ])
    mock_links.count_links = AsyncMock(return_value=0)

    results = await retriever.recall("test query", limit=10)

    # Results returned from FTS5
    assert len(results) >= 1
    # Qdrant search was never called
    mock_qdrant.search.assert_not_called()


@pytest.mark.asyncio
@patch("genesis.memory.retrieval.memory_links")
@patch("genesis.memory.retrieval.memory_crud")
@patch("genesis.memory.retrieval.qdrant_ops")
async def test_recall_fts_only_vector_rank_is_none(mock_qdrant, mock_crud, mock_links):
    """All vector_rank fields are None in FTS5-only fallback mode."""
    retriever, _, _, _ = _build_retriever(
        embed_side_effect=EmbeddingUnavailableError("all down")
    )

    mock_crud.search_ranked = AsyncMock(return_value=[
        _make_fts_row("mem-1", -5.0),
    ])
    mock_links.count_links = AsyncMock(return_value=0)

    results = await retriever.recall("test query", limit=10)
    assert len(results) == 1
    assert results[0].vector_rank is None


@pytest.mark.asyncio
@patch("genesis.memory.retrieval.memory_links")
@patch("genesis.memory.retrieval.memory_crud")
@patch("genesis.memory.retrieval.qdrant_ops")
async def test_recall_normal_unchanged(mock_qdrant, mock_crud, mock_links):
    """Normal retrieval (embedding works) still returns vector_rank values."""
    from datetime import UTC, datetime

    retriever, _, _, _ = _build_retriever()

    now = datetime.now(UTC).isoformat()
    mock_qdrant.search.return_value = [{
        "id": "mem-1",
        "score": 0.95,
        "payload": {
            "content": "hello",
            "source": "test",
            "memory_type": "episodic",
            "tags": [],
            "confidence": 0.8,
            "created_at": now,
            "retrieved_count": 2,
            "source_type": "memory",
        },
    }]
    mock_crud.search_ranked = AsyncMock(return_value=[])
    mock_links.count_links = AsyncMock(return_value=0)

    results = await retriever.recall("test query", source="episodic", limit=10)
    assert len(results) == 1
    assert results[0].vector_rank == 1
