"""Tests for capability_map CRUD and aggregation."""

from __future__ import annotations

import aiosqlite
import pytest

from genesis.db.crud import capability_map as cap_crud


@pytest.fixture
async def db(tmp_path):
    """DB with capability_map table."""
    db_path = str(tmp_path / "test.db")
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute("""
            CREATE TABLE capability_map (
                id              TEXT PRIMARY KEY,
                domain          TEXT NOT NULL UNIQUE,
                confidence      REAL NOT NULL DEFAULT 0.0,
                sample_size     INTEGER NOT NULL DEFAULT 0,
                trend           TEXT DEFAULT 'stable',
                evidence_summary TEXT,
                updated_at      TEXT NOT NULL
            )
        """)
        await conn.commit()
        yield conn


class TestUpsert:
    @pytest.mark.asyncio
    async def test_insert_new_domain(self, db):
        cid = await cap_crud.upsert(
            db, domain="investigate", confidence=0.85, sample_size=12,
            evidence_summary="journal:85%(12)",
        )
        assert isinstance(cid, str)

        entry = await cap_crud.get_by_domain(db, "investigate")
        assert entry is not None
        assert entry["confidence"] == 0.85
        assert entry["sample_size"] == 12

    @pytest.mark.asyncio
    async def test_update_existing_domain(self, db):
        await cap_crud.upsert(
            db, domain="outreach", confidence=0.5, sample_size=8,
        )
        await cap_crud.upsert(
            db, domain="outreach", confidence=0.65, sample_size=15,
            trend="improving",
        )
        entry = await cap_crud.get_by_domain(db, "outreach")
        assert entry["confidence"] == 0.65
        assert entry["sample_size"] == 15
        assert entry["trend"] == "improving"


class TestGetAll:
    @pytest.mark.asyncio
    async def test_ordered_by_confidence_desc(self, db):
        await cap_crud.upsert(db, domain="low", confidence=0.3, sample_size=5)
        await cap_crud.upsert(db, domain="high", confidence=0.9, sample_size=10)
        await cap_crud.upsert(db, domain="mid", confidence=0.6, sample_size=8)

        entries = await cap_crud.get_all(db)
        assert len(entries) == 3
        assert entries[0]["domain"] == "high"
        assert entries[1]["domain"] == "mid"
        assert entries[2]["domain"] == "low"

    @pytest.mark.asyncio
    async def test_empty_table(self, db):
        entries = await cap_crud.get_all(db)
        assert entries == []


class TestGetByDomain:
    @pytest.mark.asyncio
    async def test_found(self, db):
        await cap_crud.upsert(db, domain="research", confidence=0.75, sample_size=6)
        entry = await cap_crud.get_by_domain(db, "research")
        assert entry is not None
        assert entry["domain"] == "research"

    @pytest.mark.asyncio
    async def test_not_found(self, db):
        entry = await cap_crud.get_by_domain(db, "nonexistent")
        assert entry is None
