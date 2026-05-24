"""Tests for observation surfacing CRUD functions."""

from __future__ import annotations

from datetime import UTC, datetime

import aiosqlite
import pytest

from genesis.db.crud.observations import (
    get_standing,
    get_unsurfaced,
    mark_surfaced,
    unsurfaced_counts_by_priority,
)


@pytest.fixture
async def db(tmp_path):
    """In-memory DB with observations table including surfaced_at and surfaced_count."""
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
                surfaced_at TEXT,
                surfaced_count INTEGER NOT NULL DEFAULT 0
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

    @pytest.mark.asyncio
    async def test_surfaced_count_increments(self, db):
        """Verify surfaced_count increments on each mark_surfaced call."""
        now = datetime.now(UTC).isoformat()
        await mark_surfaced(db, ["obs-1"], now)

        cursor = await db.execute(
            "SELECT surfaced_count FROM observations WHERE id = 'obs-1'"
        )
        row = await cursor.fetchone()
        assert row[0] == 1

        # Call again — count should increment, surfaced_at preserved
        await mark_surfaced(db, ["obs-1"], datetime.now(UTC).isoformat())
        cursor = await db.execute(
            "SELECT surfaced_count, surfaced_at FROM observations WHERE id = 'obs-1'"
        )
        row = await cursor.fetchone()
        assert row[0] == 2
        assert row[1] == now  # original surfaced_at preserved via COALESCE

    @pytest.mark.asyncio
    async def test_surfaced_count_preserves_original_timestamp(self, db):
        """COALESCE preserves first surfaced_at on re-surfacing."""
        first_ts = "2026-05-20T10:00:00+00:00"
        await mark_surfaced(db, ["obs-2"], first_ts)

        second_ts = "2026-05-21T10:00:00+00:00"
        await mark_surfaced(db, ["obs-2"], second_ts)

        cursor = await db.execute(
            "SELECT surfaced_at, surfaced_count FROM observations WHERE id = 'obs-2'"
        )
        row = await cursor.fetchone()
        assert row[0] == first_ts  # first timestamp preserved
        assert row[1] == 2


class TestGetStanding:
    @pytest.mark.asyncio
    async def test_returns_standing_items(self, db):
        """Items surfaced >= threshold times are returned."""
        now = datetime.now(UTC).isoformat()
        # Surface obs-1 three times
        for _ in range(3):
            await mark_surfaced(db, ["obs-1"], now)

        items = await get_standing(db, threshold=3)
        ids = [r["id"] for r in items]
        assert "obs-1" in ids

    @pytest.mark.asyncio
    async def test_excludes_below_threshold(self, db):
        """Items surfaced fewer than threshold times are excluded."""
        now = datetime.now(UTC).isoformat()
        await mark_surfaced(db, ["obs-2"], now)  # only 1 time

        items = await get_standing(db, threshold=3)
        ids = [r["id"] for r in items]
        assert "obs-2" not in ids

    @pytest.mark.asyncio
    async def test_excludes_resolved(self, db):
        """Resolved items are excluded even if surfaced enough times."""
        # obs-6 is already resolved
        await db.execute(
            "UPDATE observations SET surfaced_count = 5 WHERE id = 'obs-6'"
        )
        await db.commit()

        items = await get_standing(db, threshold=3)
        ids = [r["id"] for r in items]
        assert "obs-6" not in ids

    @pytest.mark.asyncio
    async def test_exclude_types(self, db):
        """Excluded types are filtered out."""
        now = datetime.now(UTC).isoformat()
        # Surface obs-5 (micro_reflection) 5 times
        for _ in range(5):
            await mark_surfaced(db, ["obs-5"], now)

        items = await get_standing(db, exclude_types=("micro_reflection",), threshold=3)
        ids = [r["id"] for r in items]
        assert "obs-5" not in ids

    @pytest.mark.asyncio
    async def test_includes_surfaced_count(self, db):
        """Returned items include surfaced_count field."""
        now = datetime.now(UTC).isoformat()
        for _ in range(4):
            await mark_surfaced(db, ["obs-3"], now)

        items = await get_standing(db, threshold=3)
        obs3 = [r for r in items if r["id"] == "obs-3"]
        assert len(obs3) == 1
        assert obs3[0]["surfaced_count"] == 4


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
