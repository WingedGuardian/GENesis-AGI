"""Tests for MemoryStore embedding fallback behavior."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from genesis.memory.embeddings import EmbeddingUnavailableError
from genesis.memory.store import MemoryStore
from genesis.observability.events import GenesisEventBus


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
def event_bus():
    bus = MagicMock(spec=GenesisEventBus)
    bus.emit = AsyncMock()
    return bus


@pytest.mark.asyncio()
async def test_store_succeeds_when_embedding_fails(embedding_provider, qdrant, db, event_bus):
    """When embedding fails, content is still written to FTS5 and pending_embeddings."""
    embedding_provider.embed = AsyncMock(
        side_effect=EmbeddingUnavailableError("all down")
    )
    store = MemoryStore(
        embedding_provider=embedding_provider,
        qdrant_client=qdrant,
        db=db,
        event_bus=event_bus,
    )

    with patch("genesis.memory.store.upsert_point") as mock_upsert:
        memory_id = await store.store("test content", "test-source")

    # Memory ID returned
    assert isinstance(memory_id, str) and len(memory_id) == 36

    # Qdrant was NOT called (embedding failed before upsert_point)
    mock_upsert.assert_not_called()

    # Event was emitted
    event_bus.emit.assert_awaited_once()
    call_args = event_bus.emit.call_args
    assert call_args[0][2] == "memory.embedding_skipped"

    # Check pending_embeddings row was created
    from genesis.db.crud import pending_embeddings
    count = await pending_embeddings.count_pending(db)
    assert count == 1


@pytest.mark.asyncio()
async def test_store_normal_no_pending_row(embedding_provider, qdrant, db):
    """Normal store does not create pending_embeddings row."""
    store = MemoryStore(
        embedding_provider=embedding_provider,
        qdrant_client=qdrant,
        db=db,
    )

    with patch("genesis.memory.store.upsert_point"):
        await store.store("test content", "test-source")

    from genesis.db.crud import pending_embeddings
    count = await pending_embeddings.count_pending(db)
    assert count == 0


@pytest.mark.asyncio()
async def test_store_no_event_bus_still_works(embedding_provider, qdrant, db):
    """Embedding failure without event_bus doesn't crash."""
    embedding_provider.embed = AsyncMock(
        side_effect=EmbeddingUnavailableError("all down")
    )
    store = MemoryStore(
        embedding_provider=embedding_provider,
        qdrant_client=qdrant,
        db=db,
        event_bus=None,
    )

    with patch("genesis.memory.store.upsert_point"):
        memory_id = await store.store("test content", "test-source")

    assert isinstance(memory_id, str) and len(memory_id) == 36
