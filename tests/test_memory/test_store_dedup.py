"""Tests for MemoryStore.store() dedup behavior."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from genesis.memory.store import MemoryStore


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
async def test_store_dedup_returns_existing_id(store):
    """When exact duplicate exists, returns existing memory_id without storing."""
    with patch("genesis.memory.store.memory_crud") as mock_mem, \
         patch("genesis.memory.store.upsert_point") as mock_upsert:
        mock_mem.find_exact_duplicate = AsyncMock(return_value="existing-uuid")
        result = await store.store("duplicate content", "src")

    assert result == "existing-uuid"
    # Should NOT have called embed, upsert, or FTS write
    store._embeddings.embed.assert_not_awaited()
    mock_upsert.assert_not_called()
    mock_mem.upsert.assert_not_called()


@pytest.mark.asyncio()
async def test_store_dedup_no_match_proceeds_normally(store, embedding_provider):
    """When no duplicate exists, normal store pipeline runs."""
    with patch("genesis.memory.store.memory_crud") as mock_mem, \
         patch("genesis.memory.store.upsert_point"):
        mock_mem.find_exact_duplicate = AsyncMock(return_value=None)
        mock_mem.upsert = AsyncMock(return_value="id")
        result = await store.store("new content", "src")

    assert isinstance(result, str)
    assert len(result) == 36  # Fresh UUID
    embedding_provider.embed.assert_awaited_once()
    mock_mem.upsert.assert_awaited_once()


@pytest.mark.asyncio()
async def test_store_dedup_check_failure_continues(store, embedding_provider):
    """If dedup check raises, store proceeds normally (best-effort dedup)."""
    with patch("genesis.memory.store.memory_crud") as mock_mem, \
         patch("genesis.memory.store.upsert_point"):
        mock_mem.find_exact_duplicate = AsyncMock(
            side_effect=RuntimeError("FTS5 unavailable"),
        )
        mock_mem.upsert = AsyncMock(return_value="id")
        result = await store.store("content", "src")

    assert isinstance(result, str)
    assert len(result) == 36  # Fresh UUID despite dedup failure
    embedding_provider.embed.assert_awaited_once()


@pytest.mark.asyncio()
async def test_store_dedup_skips_auto_link(store):
    """Dedup early return should NOT call auto_link."""
    linker = MagicMock()
    linker.auto_link = AsyncMock(return_value=[])
    store._linker = linker

    with patch("genesis.memory.store.memory_crud") as mock_mem:
        mock_mem.find_exact_duplicate = AsyncMock(return_value="existing-uuid")
        await store.store("duplicate", "src")

    linker.auto_link.assert_not_awaited()
