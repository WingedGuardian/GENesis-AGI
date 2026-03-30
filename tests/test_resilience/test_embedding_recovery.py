"""Tests for EmbeddingRecoveryWorker."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from genesis.db.crud import pending_embeddings as crud
from genesis.resilience.embedding_recovery import EmbeddingRecoveryWorker


@pytest.fixture
async def worker(db):
    embedder = AsyncMock()
    embedder.embed = AsyncMock(return_value=[0.1] * 1024)
    qdrant = MagicMock()
    w = EmbeddingRecoveryWorker(
        db=db,
        embedding_provider=embedder,
        qdrant_client=qdrant,
        pace_per_min=0,  # No delay in tests
    )
    return w, embedder, qdrant


class TestDrainPending:
    @pytest.mark.asyncio
    async def test_empty_queue_returns_zero(self, worker):
        w, _, _ = worker
        assert await w.drain_pending() == 0

    @pytest.mark.asyncio
    async def test_successful_drain(self, db, worker):
        w, embedder, qdrant = worker
        # Insert pending items
        await crud.create(
            db, id="pe-1", memory_id="mem-1", content="hello world",
            memory_type="episodic", collection="episodic_memory",
            created_at="2026-03-11T12:00:00",
        )
        await crud.create(
            db, id="pe-2", memory_id="mem-2", content="second item",
            memory_type="semantic", collection="episodic_memory",
            created_at="2026-03-11T12:00:01", tags="tag1,tag2",
        )

        count = await w.drain_pending()
        assert count == 2
        assert embedder.embed.call_count == 2
        assert qdrant.upsert.call_count == 2  # upsert_point calls qdrant.upsert internally
        assert await crud.count_pending(db) == 0

    @pytest.mark.asyncio
    async def test_partial_failure(self, db, worker):
        w, embedder, qdrant = worker
        await crud.create(
            db, id="pe-1", memory_id="mem-1", content="good",
            memory_type="episodic", collection="episodic_memory",
            created_at="2026-03-11T12:00:00",
        )
        await crud.create(
            db, id="pe-2", memory_id="mem-2", content="bad",
            memory_type="episodic", collection="episodic_memory",
            created_at="2026-03-11T12:00:01",
        )

        # Second embed call fails
        embedder.embed = AsyncMock(side_effect=[
            [0.1] * 1024,
            RuntimeError("provider down"),
        ])

        count = await w.drain_pending()
        assert count == 1  # Only first succeeded

        # Check second item marked failed
        pending = await crud.query_pending(db)
        assert len(pending) == 0  # None pending (one embedded, one failed)

    @pytest.mark.asyncio
    async def test_count_pending(self, db, worker):
        w, _, _ = worker
        await crud.create(
            db, id="pe-1", memory_id="mem-1", content="test",
            memory_type="episodic", collection="episodic_memory",
            created_at="2026-03-11T12:00:00",
        )
        assert await w.count_pending() == 1

    @pytest.mark.asyncio
    async def test_with_linker(self, db):
        embedder = AsyncMock()
        embedder.embed = AsyncMock(return_value=[0.1] * 1024)
        qdrant = MagicMock()
        linker = AsyncMock()
        linker.auto_link = AsyncMock()

        w = EmbeddingRecoveryWorker(
            db=db, embedding_provider=embedder, qdrant_client=qdrant,
            linker=linker, pace_per_min=0,
        )

        await crud.create(
            db, id="pe-1", memory_id="mem-1", content="test",
            memory_type="episodic", collection="episodic_memory",
            created_at="2026-03-11T12:00:00",
        )

        count = await w.drain_pending()
        assert count == 1
        linker.auto_link.assert_called_once()

    @pytest.mark.asyncio
    async def test_limit(self, db, worker):
        w, _, _ = worker
        for i in range(5):
            await crud.create(
                db, id=f"pe-{i}", memory_id=f"mem-{i}", content=f"item {i}",
                memory_type="episodic", collection="episodic_memory",
                created_at=f"2026-03-11T12:00:0{i}",
            )
        count = await w.drain_pending(limit=2)
        assert count == 2
        assert await w.count_pending() == 3
