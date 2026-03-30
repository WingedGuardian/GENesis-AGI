"""Tests for pending_embeddings CRUD operations."""

from __future__ import annotations

import pytest

from genesis.db.crud import pending_embeddings as crud


class TestCreate:
    @pytest.mark.asyncio
    async def test_create_returns_id(self, db):
        result = await crud.create(
            db, id="pe-1", memory_id="mem-1", content="hello",
            memory_type="episodic", collection="episodic_memory",
            created_at="2026-03-11T12:00:00",
        )
        assert result == "pe-1"

    @pytest.mark.asyncio
    async def test_create_with_tags(self, db):
        await crud.create(
            db, id="pe-1", memory_id="mem-1", content="hello",
            memory_type="episodic", collection="episodic_memory",
            created_at="2026-03-11T12:00:00", tags="tag1,tag2",
        )
        items = await crud.query_pending(db)
        assert items[0]["tags"] == "tag1,tag2"


class TestQueryPending:
    @pytest.mark.asyncio
    async def test_returns_pending_only(self, db):
        await crud.create(
            db, id="pe-1", memory_id="mem-1", content="a",
            memory_type="episodic", collection="episodic_memory",
            created_at="2026-03-11T12:00:00",
        )
        await crud.create(
            db, id="pe-2", memory_id="mem-2", content="b",
            memory_type="episodic", collection="episodic_memory",
            created_at="2026-03-11T12:00:01",
        )
        await crud.mark_embedded(db, "pe-1", embedded_at="2026-03-11T12:01:00")
        items = await crud.query_pending(db)
        assert len(items) == 1
        assert items[0]["id"] == "pe-2"

    @pytest.mark.asyncio
    async def test_ordered_by_created_at(self, db):
        await crud.create(
            db, id="pe-2", memory_id="mem-2", content="second",
            memory_type="episodic", collection="episodic_memory",
            created_at="2026-03-11T12:00:01",
        )
        await crud.create(
            db, id="pe-1", memory_id="mem-1", content="first",
            memory_type="episodic", collection="episodic_memory",
            created_at="2026-03-11T12:00:00",
        )
        items = await crud.query_pending(db)
        assert items[0]["id"] == "pe-1"


class TestMarkEmbedded:
    @pytest.mark.asyncio
    async def test_mark_embedded(self, db):
        await crud.create(
            db, id="pe-1", memory_id="mem-1", content="test",
            memory_type="episodic", collection="episodic_memory",
            created_at="2026-03-11T12:00:00",
        )
        assert await crud.mark_embedded(db, "pe-1", embedded_at="2026-03-11T12:01:00")
        assert await crud.count_pending(db) == 0

    @pytest.mark.asyncio
    async def test_mark_nonexistent(self, db):
        assert not await crud.mark_embedded(db, "nonexistent", embedded_at="2026-03-11T12:01:00")


class TestMarkFailed:
    @pytest.mark.asyncio
    async def test_mark_failed(self, db):
        await crud.create(
            db, id="pe-1", memory_id="mem-1", content="test",
            memory_type="episodic", collection="episodic_memory",
            created_at="2026-03-11T12:00:00",
        )
        assert await crud.mark_failed(db, "pe-1", error_message="provider down")
        assert await crud.count_pending(db) == 0


class TestResetFailedToPending:
    @pytest.mark.asyncio
    async def test_resets_all_failed(self, db):
        await crud.create(
            db, id="pe-1", memory_id="mem-1", content="a",
            memory_type="episodic", collection="episodic_memory",
            created_at="2026-03-11T12:00:00",
        )
        await crud.mark_failed(db, "pe-1", error_message="some error")
        count = await crud.reset_failed_to_pending(db)
        assert count == 1
        assert await crud.count_pending(db) == 1

    @pytest.mark.asyncio
    async def test_resets_with_error_filter(self, db):
        await crud.create(
            db, id="pe-1", memory_id="mem-1", content="a",
            memory_type="episodic", collection="episodic_memory",
            created_at="2026-03-11T12:00:00",
        )
        await crud.create(
            db, id="pe-2", memory_id="mem-2", content="b",
            memory_type="episodic", collection="episodic_memory",
            created_at="2026-03-11T12:00:01",
        )
        await crud.mark_failed(db, "pe-1", error_message="OBSERVABILITY missing")
        await crud.mark_failed(db, "pe-2", error_message="connection refused")
        count = await crud.reset_failed_to_pending(db, error_filter="OBSERVABILITY")
        assert count == 1
        assert await crud.count_pending(db) == 1

    @pytest.mark.asyncio
    async def test_clears_error_message(self, db):
        await crud.create(
            db, id="pe-1", memory_id="mem-1", content="a",
            memory_type="episodic", collection="episodic_memory",
            created_at="2026-03-11T12:00:00",
        )
        await crud.mark_failed(db, "pe-1", error_message="old error")
        await crud.reset_failed_to_pending(db)
        items = await crud.query_pending(db)
        assert items[0]["error_message"] is None


class TestCountPending:
    @pytest.mark.asyncio
    async def test_count_empty(self, db):
        assert await crud.count_pending(db) == 0

    @pytest.mark.asyncio
    async def test_count_multiple(self, db):
        for i in range(3):
            await crud.create(
                db, id=f"pe-{i}", memory_id=f"mem-{i}", content=f"item {i}",
                memory_type="episodic", collection="episodic_memory",
                created_at=f"2026-03-11T12:00:0{i}",
            )
        assert await crud.count_pending(db) == 3
