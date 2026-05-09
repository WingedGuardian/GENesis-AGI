"""Tests for observation surfacing CRUD functions."""

from __future__ import annotations

from datetime import UTC, datetime

import aiosqlite
import pytest

from genesis.db.crud.observations import (
    get_unsurfaced,
    mark_surfaced,
    unsurfaced_counts_by_priority,
)


@pytest.fixture
async def db(tmp_path):
    """In-memory DB with observations table including surfaced_at."""
    db_path = str(tmp_path / "test.db")
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute("""
            CREATE TABLE observations (
                id TEXT PRIMARY KEY,
                person_id TEXT,
                source TEXT,
                type TEXT,
                category TEXT,
                content TEXT,
                priority TEXT,
                speculative INTEGER DEFAULT 0,
                retrieved_count INTEGER DEFAULT 0,
                influenced_action INTEGER DEFAULT 0,
                resolved INTEGER DEFAULT 0,
                resolved_at TEXT,
                resolution_notes TEXT,
                created_at TEXT,
                expires_at TEXT,
                content_hash TEXT,
                surfaced_at TEXT
            )
        """)
        # Seed test data
        now = datetime.now(UTC).isoformat()
        rows = [
            ("obs-1", "sys", "finding", None, "Critical issue found", "critical", 0, now),
            ("obs-2", "sys", "finding", None, "High priority item", "high", 0, now),
            ("obs-3", "sys", "finding", None, "Medium finding", "medium", 0, now),
            ("obs-4", "sys", "finding", None, "Low finding", "low", 0, now),
            ("obs-5", "sys", "micro_reflection", None, "Internal stuff", "medium", 0, now),
            ("obs-6", "sys", "finding", None, "Already resolved", "high", 1, now),
            ("obs-7", "sys", "finding", None, "Already surfaced", "high", 0, now),
        ]
        for oid, source, otype, cat, content, prio, resolved, created in rows:
            await conn.execute(
                "INSERT INTO observations (id, source, type, category, content, priority, resolved, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (oid, source, otype, cat, content, prio, resolved, created),
            )
        # Mark obs-7 as already surfaced
        await conn.execute(
            "UPDATE observations SET surfaced_at = ? WHERE id = 'obs-7'",
            (now,),
        )
        await conn.commit()
        yield conn


class TestGetUnsurfaced:
    @pytest.mark.asyncio
    async def test_returns_unsurfaced_unresolved(self, db):
        results = await get_unsurfaced(db)
        ids = [r["id"] for r in results]
        # Should include obs-1 (critical), obs-2 (high), obs-3 (medium)
        assert "obs-1" in ids
        assert "obs-2" in ids
        assert "obs-3" in ids
        # Should NOT include:
        assert "obs-4" not in ids  # low priority (not in filter)
        assert "obs-6" not in ids  # resolved
        assert "obs-7" not in ids  # already surfaced

    @pytest.mark.asyncio
    async def test_priority_ordering(self, db):
        results = await get_unsurfaced(db)
        priorities = [r["priority"] for r in results]
        # Critical should come first
        assert priorities[0] == "critical"

    @pytest.mark.asyncio
    async def test_exclude_types(self, db):
        results = await get_unsurfaced(
            db, exclude_types=("micro_reflection",),
        )
        ids = [r["id"] for r in results]
        assert "obs-5" not in ids

    @pytest.mark.asyncio
    async def test_limit(self, db):
        results = await get_unsurfaced(db, limit=1)
        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_include_low_priority(self, db):
        results = await get_unsurfaced(
            db, priority_filter=("critical", "high", "medium", "low"),
        )
        ids = [r["id"] for r in results]
        assert "obs-4" in ids


class TestMarkSurfaced:
    @pytest.mark.asyncio
    async def test_marks_observations(self, db):
        now = datetime.now(UTC).isoformat()
        count = await mark_surfaced(db, ["obs-1", "obs-2"], now)
        assert count == 2

        # Verify they're now surfaced
        results = await get_unsurfaced(db)
        ids = [r["id"] for r in results]
        assert "obs-1" not in ids
        assert "obs-2" not in ids

    @pytest.mark.asyncio
    async def test_empty_list(self, db):
        count = await mark_surfaced(db, [], datetime.now(UTC).isoformat())
        assert count == 0


class TestUnsurfacedCounts:
    @pytest.mark.asyncio
    async def test_counts_by_priority(self, db):
        counts = await unsurfaced_counts_by_priority(db)
        assert counts.get("critical") == 1
        assert counts.get("high") == 1  # obs-2 only (obs-6 resolved, obs-7 surfaced)
        assert counts.get("medium") == 2  # obs-3 + obs-5
        assert counts.get("low") == 1

    @pytest.mark.asyncio
    async def test_counts_decrease_after_surfacing(self, db):
        await mark_surfaced(db, ["obs-1"], datetime.now(UTC).isoformat())
        counts = await unsurfaced_counts_by_priority(db)
        assert counts.get("critical", 0) == 0
