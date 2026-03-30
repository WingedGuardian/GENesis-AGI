"""Tests for ego CRUD operations (ego_cycles + ego_state)."""

from __future__ import annotations

import asyncio

import aiosqlite
import pytest

from genesis.db.crud import ego as ego_crud
from genesis.db.schema import TABLES

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def db():
    """In-memory DB with ego tables."""
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute(TABLES["ego_cycles"])
        await conn.execute(TABLES["ego_state"])
        yield conn


def _make_cycle_kwargs(id: str, created_at: str = "2026-03-28T10:00:00Z", **overrides):
    """Helper to build create_cycle kwargs."""
    base = {
        "id": id,
        "output_text": f"output for {id}",
        "proposals_json": "[]",
        "focus_summary": f"focus {id}",
        "model_used": "test-model",
        "cost_usd": 0.01,
        "input_tokens": 100,
        "output_tokens": 50,
        "duration_ms": 500,
        "created_at": created_at,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# ego_cycles tests
# ---------------------------------------------------------------------------


class TestCyclesCRUD:
    async def test_create_and_get_roundtrip(self, db):
        kw = _make_cycle_kwargs("c1")
        returned_id = await ego_crud.create_cycle(db, **kw)
        assert returned_id == "c1"

        row = await ego_crud.get_cycle(db, "c1")
        assert row is not None
        assert row["id"] == "c1"
        assert row["output_text"] == "output for c1"
        assert row["model_used"] == "test-model"

    async def test_get_cycle_missing(self, db):
        assert await ego_crud.get_cycle(db, "nonexistent") is None

    async def test_list_recent_cycles_ordering(self, db):
        """Newest first."""
        for i, ts in enumerate(["2026-01-01", "2026-01-03", "2026-01-02"]):
            await ego_crud.create_cycle(db, **_make_cycle_kwargs(f"c{i}", ts))

        rows = await ego_crud.list_recent_cycles(db, limit=10)
        timestamps = [r["created_at"] for r in rows]
        assert timestamps == ["2026-01-03", "2026-01-02", "2026-01-01"]

    async def test_list_recent_cycles_limit(self, db):
        for i in range(5):
            await ego_crud.create_cycle(
                db, **_make_cycle_kwargs(f"c{i}", f"2026-01-0{i + 1}")
            )
        rows = await ego_crud.list_recent_cycles(db, limit=2)
        assert len(rows) == 2

    async def test_uncompacted_beyond_window_empty_table(self, db):
        rows = await ego_crud.list_uncompacted_beyond_window(db, window_size=3)
        assert rows == []

    async def test_uncompacted_beyond_window_all_compacted(self, db):
        for i in range(5):
            kw = _make_cycle_kwargs(f"c{i}", f"2026-01-0{i + 1}")
            await ego_crud.create_cycle(db, **kw)
            await ego_crud.mark_compacted(db, cycle_id=f"c{i}", compacted_into="summary")

        rows = await ego_crud.list_uncompacted_beyond_window(db, window_size=3)
        assert rows == []

    async def test_uncompacted_at_window(self, db):
        """Exactly window_size uncompacted → nothing to compact."""
        for i in range(3):
            await ego_crud.create_cycle(
                db, **_make_cycle_kwargs(f"c{i}", f"2026-01-0{i + 1}")
            )
        rows = await ego_crud.list_uncompacted_beyond_window(db, window_size=3)
        assert rows == []

    async def test_uncompacted_above_window(self, db):
        """window_size + 1 → returns oldest 1."""
        for i in range(4):
            await ego_crud.create_cycle(
                db, **_make_cycle_kwargs(f"c{i}", f"2026-01-0{i + 1}")
            )
        rows = await ego_crud.list_uncompacted_beyond_window(db, window_size=3)
        assert len(rows) == 1
        assert rows[0]["id"] == "c0"

    async def test_uncompacted_multiple_above_window(self, db):
        """window_size + 3 → returns oldest 3, oldest first."""
        for i in range(6):
            await ego_crud.create_cycle(
                db, **_make_cycle_kwargs(f"c{i}", f"2026-01-0{i + 1}")
            )
        rows = await ego_crud.list_uncompacted_beyond_window(db, window_size=3)
        assert len(rows) == 3
        assert [r["id"] for r in rows] == ["c0", "c1", "c2"]

    async def test_uncompacted_tiebreaking(self, db):
        """Same created_at → deterministic by id."""
        same_ts = "2026-01-01T00:00:00Z"
        for cid in ["aaa", "bbb", "ccc", "ddd"]:
            await ego_crud.create_cycle(
                db, **_make_cycle_kwargs(cid, same_ts)
            )
        rows = await ego_crud.list_uncompacted_beyond_window(db, window_size=2)
        # Window keeps the 2 largest ids (ddd, ccc). Returns aaa, bbb.
        assert [r["id"] for r in rows] == ["aaa", "bbb"]

    async def test_mark_compacted(self, db):
        await ego_crud.create_cycle(db, **_make_cycle_kwargs("c1"))
        result = await ego_crud.mark_compacted(
            db, cycle_id="c1", compacted_into="summary_v1"
        )
        assert result is True

        row = await ego_crud.get_cycle(db, "c1")
        assert row["compacted_into"] == "summary_v1"

    async def test_mark_compacted_nonexistent(self, db):
        result = await ego_crud.mark_compacted(
            db, cycle_id="nope", compacted_into="summary"
        )
        assert result is False

    async def test_count_uncompacted(self, db):
        for i in range(5):
            await ego_crud.create_cycle(
                db, **_make_cycle_kwargs(f"c{i}", f"2026-01-0{i + 1}")
            )
        assert await ego_crud.count_uncompacted(db) == 5

        await ego_crud.mark_compacted(db, cycle_id="c0", compacted_into="s")
        await ego_crud.mark_compacted(db, cycle_id="c1", compacted_into="s")
        assert await ego_crud.count_uncompacted(db) == 3


# ---------------------------------------------------------------------------
# ego_state tests
# ---------------------------------------------------------------------------


class TestStateCRUD:
    async def test_get_state_missing(self, db):
        assert await ego_crud.get_state(db, "nonexistent") is None

    async def test_set_and_get(self, db):
        await ego_crud.set_state(db, key="foo", value="bar")
        assert await ego_crud.get_state(db, "foo") == "bar"

    async def test_upsert_updates_value(self, db):
        await ego_crud.set_state(db, key="k", value="v1")
        await ego_crud.set_state(db, key="k", value="v2")
        assert await ego_crud.get_state(db, "k") == "v2"

    async def test_upsert_updates_timestamp(self, db):
        """Verify ON CONFLICT updates updated_at (the INSERT OR REPLACE bug)."""
        await ego_crud.set_state(db, key="k", value="v1")

        cursor = await db.execute(
            "SELECT updated_at FROM ego_state WHERE key = 'k'"
        )
        row1 = await cursor.fetchone()
        ts1 = row1[0]

        # Small delay to ensure datetime('now') changes
        await asyncio.sleep(1.1)

        await ego_crud.set_state(db, key="k", value="v2")

        cursor = await db.execute(
            "SELECT updated_at FROM ego_state WHERE key = 'k'"
        )
        row2 = await cursor.fetchone()
        ts2 = row2[0]

        assert ts2 > ts1, f"updated_at should change on upsert: {ts1} -> {ts2}"


class TestDailyEgoCost:
    async def test_no_cycles_returns_zero(self, db):
        cost = await ego_crud.daily_ego_cost(db)
        assert cost == 0.0

    async def test_sums_todays_cycles(self, db):
        from datetime import UTC, datetime

        today = datetime.now(UTC).strftime("%Y-%m-%d")
        for i in range(3):
            await ego_crud.create_cycle(
                db, id=f"c{i}", output_text="x",
                cost_usd=0.25,
                created_at=f"{today}T12:00:00",
            )
        cost = await ego_crud.daily_ego_cost(db)
        assert abs(cost - 0.75) < 0.001

    async def test_excludes_other_dates(self, db):
        await ego_crud.create_cycle(
            db, id="old", output_text="x", cost_usd=1.0,
            created_at="2020-01-01T00:00:00",
        )
        await ego_crud.create_cycle(
            db, id="today", output_text="x", cost_usd=0.5,
            created_at="2026-03-28T10:00:00",
        )
        cost = await ego_crud.daily_ego_cost(db, date="2026-03-28")
        assert abs(cost - 0.5) < 0.001
