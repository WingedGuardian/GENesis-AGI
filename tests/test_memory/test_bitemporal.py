"""Tests for bi-temporal memory wiring (valid_at / invalid_at)."""

from __future__ import annotations

import pytest

from genesis.memory.extraction import Extraction, extractions_to_store_kwargs

# ---------------------------------------------------------------------------
# Extraction → valid_at mapping
# ---------------------------------------------------------------------------


class TestExtractionValidAt:
    """Verify extraction temporal field maps to valid_at in store kwargs."""

    def test_temporal_becomes_valid_at(self):
        ext = Extraction(
            content="PR #773 was closed",
            extraction_type="entity",
            confidence=0.9,
            temporal="2026-05-03",
        )
        kwargs = extractions_to_store_kwargs(ext)
        assert kwargs["valid_at"] == "2026-05-03"

    def test_no_temporal_gives_none_valid_at(self):
        ext = Extraction(
            content="Genesis uses RRF fusion",
            extraction_type="concept",
            confidence=0.8,
        )
        kwargs = extractions_to_store_kwargs(ext)
        assert kwargs["valid_at"] is None

    def test_temporal_still_in_tags(self):
        """Temporal should appear in BOTH tags and valid_at."""
        ext = Extraction(
            content="Something happened",
            extraction_type="entity",
            confidence=0.7,
            temporal="2026-04-15",
        )
        kwargs = extractions_to_store_kwargs(ext)
        assert "2026-04-15" in kwargs["tags"]
        assert kwargs["valid_at"] == "2026-04-15"


# ---------------------------------------------------------------------------
# CRUD: create_metadata with bi-temporal columns
# ---------------------------------------------------------------------------


@pytest.fixture
async def meta_db():
    """In-memory SQLite with memory_metadata table."""
    import aiosqlite
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await db.execute("""
        CREATE TABLE memory_metadata (
            memory_id        TEXT PRIMARY KEY,
            created_at       TEXT NOT NULL,
            collection       TEXT NOT NULL DEFAULT 'episodic_memory',
            confidence       REAL,
            embedding_status TEXT NOT NULL DEFAULT 'embedded',
            memory_class     TEXT DEFAULT 'fact',
            wing             TEXT,
            room             TEXT,
            valid_at         TEXT,
            invalid_at       TEXT
        )
    """)
    await db.commit()
    yield db
    await db.close()


class TestCreateMetadata:
    """Tests for create_metadata with bi-temporal columns."""

    @pytest.mark.asyncio
    async def test_valid_at_defaults_to_created_at(self, meta_db):
        from genesis.db.crud.memory import create_metadata
        await create_metadata(
            meta_db,
            memory_id="m1",
            created_at="2026-05-08T12:00:00Z",
        )
        cursor = await meta_db.execute(
            "SELECT valid_at, invalid_at FROM memory_metadata WHERE memory_id = ?",
            ("m1",),
        )
        row = await cursor.fetchone()
        assert row["valid_at"] == "2026-05-08T12:00:00Z"
        assert row["invalid_at"] is None

    @pytest.mark.asyncio
    async def test_explicit_valid_at(self, meta_db):
        from genesis.db.crud.memory import create_metadata
        await create_metadata(
            meta_db,
            memory_id="m2",
            created_at="2026-05-08T12:00:00Z",
            valid_at="2026-04-15",
        )
        cursor = await meta_db.execute(
            "SELECT valid_at FROM memory_metadata WHERE memory_id = ?",
            ("m2",),
        )
        row = await cursor.fetchone()
        assert row["valid_at"] == "2026-04-15"

    @pytest.mark.asyncio
    async def test_explicit_invalid_at(self, meta_db):
        from genesis.db.crud.memory import create_metadata
        await create_metadata(
            meta_db,
            memory_id="m3",
            created_at="2026-05-08T12:00:00Z",
            invalid_at="2026-05-07",
        )
        cursor = await meta_db.execute(
            "SELECT invalid_at FROM memory_metadata WHERE memory_id = ?",
            ("m3",),
        )
        row = await cursor.fetchone()
        assert row["invalid_at"] == "2026-05-07"


class TestInvalidateMemory:
    """Tests for invalidate_memory()."""

    @pytest.mark.asyncio
    async def test_invalidate_sets_invalid_at(self, meta_db):
        from genesis.db.crud.memory import create_metadata, invalidate_memory
        await create_metadata(
            meta_db, memory_id="m1", created_at="2026-05-08T12:00:00Z",
        )
        result = await invalidate_memory(meta_db, "m1", "2026-05-08T15:00:00Z")
        assert result is True

        cursor = await meta_db.execute(
            "SELECT invalid_at FROM memory_metadata WHERE memory_id = ?",
            ("m1",),
        )
        row = await cursor.fetchone()
        assert row["invalid_at"] == "2026-05-08T15:00:00Z"

    @pytest.mark.asyncio
    async def test_invalidate_nonexistent_returns_false(self, meta_db):
        from genesis.db.crud.memory import invalidate_memory
        result = await invalidate_memory(meta_db, "nonexistent", "2026-05-08")
        assert result is False
