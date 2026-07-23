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
                previous_confidence REAL,
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



class TestGetWeakest:
    @pytest.mark.asyncio
    async def test_weakest_first_below_threshold(self, db):
        await cap_crud.upsert(db, domain="strong", confidence=0.9, sample_size=10)
        await cap_crud.upsert(db, domain="weak1", confidence=0.2, sample_size=8)
        await cap_crud.upsert(db, domain="weak2", confidence=0.35, sample_size=8)

        weak = await cap_crud.get_weakest(db, max_confidence=0.5)
        assert [e["domain"] for e in weak] == ["weak1", "weak2"]

    @pytest.mark.asyncio
    async def test_min_sample_size_filters_flukes(self, db):
        await cap_crud.upsert(db, domain="fluke", confidence=0.1, sample_size=1)
        await cap_crud.upsert(db, domain="real", confidence=0.3, sample_size=5)

        weak = await cap_crud.get_weakest(db, max_confidence=0.5, min_sample_size=3)
        assert [e["domain"] for e in weak] == ["real"]

    @pytest.mark.asyncio
    async def test_limit_caps_results(self, db):
        for i in range(5):
            await cap_crud.upsert(
                db, domain=f"d{i}", confidence=0.1 + i * 0.05, sample_size=5,
            )
        weak = await cap_crud.get_weakest(db, max_confidence=0.5, limit=2)
        assert len(weak) == 2
        assert weak[0]["domain"] == "d0"  # lowest confidence first

    @pytest.mark.asyncio
    async def test_empty_when_all_strong(self, db):
        await cap_crud.upsert(db, domain="strong", confidence=0.95, sample_size=10)
        weak = await cap_crud.get_weakest(db, max_confidence=0.5)
        assert weak == []
