"""Tests for ObservationWriter — dual-write to DB + optional MemoryStore."""

from __future__ import annotations

from unittest.mock import AsyncMock

import aiosqlite
import pytest

from genesis.db import schema
from genesis.learning.observation_writer import ObservationWriter


@pytest.fixture
async def db():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        for ddl in schema.TABLES.values():
            await conn.execute(ddl)
        await conn.commit()
        yield conn


class TestObservationWriter:
    @pytest.mark.asyncio
    async def test_write_creates_observation(self, db):
        writer = ObservationWriter()
        obs_id = await writer.write(
            db, source="retrospective", type="test_obs", content="hello", priority="medium"
        )
        assert obs_id  # non-empty UUID string
        cursor = await db.execute("SELECT * FROM observations WHERE id = ?", (obs_id,))
        row = dict(await cursor.fetchone())
        assert row["source"] == "retrospective"
        assert row["type"] == "test_obs"
        assert row["content"] == "hello"
        assert row["priority"] == "medium"

    @pytest.mark.asyncio
    async def test_write_with_category(self, db):
        writer = ObservationWriter()
        obs_id = await writer.write(
            db, source="test", type="t", content="c", priority="low", category="learning"
        )
        cursor = await db.execute("SELECT category FROM observations WHERE id = ?", (obs_id,))
        row = dict(await cursor.fetchone())
        assert row["category"] == "learning"

    @pytest.mark.asyncio
    async def test_dual_write_with_memory_store(self, db):
        store = AsyncMock()
        store.store.return_value = "mem-1"
        writer = ObservationWriter(memory_store=store)
        await writer.write(
            db, source="retro", type="t", content="data", priority="high"
        )
        store.store.assert_awaited_once()
        call_args = store.store.call_args
        assert call_args[0][0] == "data"       # content
        assert call_args[0][1] == "retro"      # source
        assert call_args[1]["memory_type"] == "episodic"
        assert "t" in call_args[1]["tags"]
        assert any(t.startswith("obs:") for t in call_args[1]["tags"])

    @pytest.mark.asyncio
    async def test_memory_store_failure_is_nonfatal(self, db):
        store = AsyncMock()
        store.store.side_effect = RuntimeError("store down")
        writer = ObservationWriter(memory_store=store)
        # Should not raise
        obs_id = await writer.write(
            db, source="retro", type="t", content="data", priority="high"
        )
        assert obs_id
        # Observation should still be in DB
        cursor = await db.execute("SELECT * FROM observations WHERE id = ?", (obs_id,))
        assert await cursor.fetchone() is not None

    @pytest.mark.asyncio
    async def test_no_memory_store(self, db):
        writer = ObservationWriter(memory_store=None)
        obs_id = await writer.write(
            db, source="test", type="t", content="c", priority="low"
        )
        assert obs_id

    @pytest.mark.asyncio
    async def test_skip_embed_types_not_stored_in_memory(self, db):
        """Low-value observation types skip the MemoryStore embed."""
        from genesis.learning.observation_writer import _SKIP_EMBED_TYPES

        store = AsyncMock()
        store.store.return_value = "mem-1"
        writer = ObservationWriter(memory_store=store)
        for skip_type in _SKIP_EMBED_TYPES:
            await writer.write(
                db, source="test", type=skip_type, content="data", priority="low"
            )
        store.store.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_non_skip_types_still_stored_in_memory(self, db):
        """Types not in the skip set still get embedded via MemoryStore."""
        store = AsyncMock()
        store.store.return_value = "mem-1"
        writer = ObservationWriter(memory_store=store)
        await writer.write(
            db, source="test", type="light_reflection", content="data", priority="medium"
        )
        store.store.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_skip_embed_types_still_written_to_db(self, db):
        """Skipped types are still persisted to the observations table."""
        store = AsyncMock()
        writer = ObservationWriter(memory_store=store)
        obs_id = await writer.write(
            db, source="test", type="memory_operation", content="data", priority="low"
        )
        cursor = await db.execute("SELECT * FROM observations WHERE id = ?", (obs_id,))
        assert await cursor.fetchone() is not None


class TestObservationDedup:
    async def test_duplicate_observation_skipped(self, db):
        """Writing the same observation twice skips the duplicate."""
        writer = ObservationWriter()
        id1 = await writer.write(
            db, source="test", type="test_obs", content="hello world", priority="medium",
        )
        id2 = await writer.write(
            db, source="test", type="test_obs", content="hello world", priority="medium",
        )
        # Both return IDs but only one row exists
        assert id1 is not None
        assert id2 is not None
        cursor = await db.execute(
            "SELECT COUNT(*) FROM observations WHERE source = 'test' AND type = 'test_obs'"
        )
        count = (await cursor.fetchone())[0]
        assert count == 1

    async def test_numeric_variation_deduped(self, db):
        """Observations differing only by numbers are deduped."""
        writer = ObservationWriter()
        await writer.write(
            db, source="test", type="metric", content="staleness=0.945", priority="low",
        )
        await writer.write(
            db, source="test", type="metric", content="staleness=1.0", priority="low",
        )
        cursor = await db.execute(
            "SELECT COUNT(*) FROM observations WHERE source = 'test' AND type = 'metric'"
        )
        count = (await cursor.fetchone())[0]
        assert count == 1

    async def test_different_types_not_deduped(self, db):
        """Same content with different types are distinct."""
        writer = ObservationWriter()
        await writer.write(
            db, source="test", type="type_a", content="hello", priority="medium",
        )
        await writer.write(
            db, source="test", type="type_b", content="hello", priority="medium",
        )
        cursor = await db.execute(
            "SELECT COUNT(*) FROM observations WHERE source = 'test'"
        )
        count = (await cursor.fetchone())[0]
        assert count == 2

    async def test_explicit_content_hash_respected(self, db):
        """When caller provides content_hash, it's used as-is."""
        writer = ObservationWriter()
        await writer.write(
            db, source="test", type="test_obs", content="hello",
            priority="medium", content_hash="custom_hash_123",
        )
        cursor = await db.execute(
            "SELECT content_hash FROM observations WHERE source = 'test'"
        )
        row = await cursor.fetchone()
        assert row[0] == "custom_hash_123"
