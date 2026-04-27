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


# ---------------------------------------------------------------------------
# ego_proposals tests
# ---------------------------------------------------------------------------


@pytest.fixture
async def db_with_proposals():
    """In-memory DB with ego_proposals table."""
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute(TABLES["ego_proposals"])
        yield conn


def _make_proposal_kwargs(id: str, **overrides):
    base = {
        "id": id,
        "action_type": "research",
        "action_category": "learning",
        "content": f"proposal {id}",
        "rationale": "test rationale",
        "confidence": 0.8,
        "urgency": "normal",
        "status": "pending",
        "created_at": "2026-04-20T10:00:00Z",
    }
    base.update(overrides)
    return base


class TestListProposals:
    async def test_empty_table(self, db_with_proposals):
        result = await ego_crud.list_proposals(db_with_proposals)
        assert result == []

    async def test_returns_all_proposals(self, db_with_proposals):
        for i in range(3):
            await ego_crud.create_proposal(
                db_with_proposals,
                **_make_proposal_kwargs(f"p{i}", created_at=f"2026-04-2{i}T10:00:00Z"),
            )
        result = await ego_crud.list_proposals(db_with_proposals)
        assert len(result) == 3

    async def test_newest_first(self, db_with_proposals):
        for i, ts in enumerate(["2026-01-01", "2026-01-03", "2026-01-02"]):
            await ego_crud.create_proposal(
                db_with_proposals,
                **_make_proposal_kwargs(f"p{i}", created_at=f"{ts}T00:00:00Z"),
            )
        result = await ego_crud.list_proposals(db_with_proposals)
        assert result[0]["id"] == "p1"  # Jan 3 is newest
        assert result[-1]["id"] == "p0"  # Jan 1 is oldest

    async def test_filter_by_status(self, db_with_proposals):
        await ego_crud.create_proposal(
            db_with_proposals, **_make_proposal_kwargs("p1", status="pending"),
        )
        await ego_crud.create_proposal(
            db_with_proposals, **_make_proposal_kwargs("p2", status="pending"),
        )
        await ego_crud.create_proposal(
            db_with_proposals, **_make_proposal_kwargs("p3", status="approved"),
        )
        pending = await ego_crud.list_proposals(db_with_proposals, status="pending")
        assert len(pending) == 2
        approved = await ego_crud.list_proposals(db_with_proposals, status="approved")
        assert len(approved) == 1
        assert approved[0]["id"] == "p3"

    async def test_limit(self, db_with_proposals):
        for i in range(5):
            await ego_crud.create_proposal(
                db_with_proposals,
                **_make_proposal_kwargs(f"p{i}", created_at=f"2026-04-2{i}T10:00:00Z"),
            )
        result = await ego_crud.list_proposals(db_with_proposals, limit=2)
        assert len(result) == 2


class TestCreateProposalNewFields:
    async def test_rank_stored(self, db_with_proposals):
        await ego_crud.create_proposal(
            db_with_proposals,
            **_make_proposal_kwargs("p1", rank=1),
        )
        row = await ego_crud.get_proposal(db_with_proposals, "p1")
        assert row["rank"] == 1

    async def test_execution_plan_stored(self, db_with_proposals):
        await ego_crud.create_proposal(
            db_with_proposals,
            **_make_proposal_kwargs("p1", execution_plan="background CC, ~$0.50"),
        )
        row = await ego_crud.get_proposal(db_with_proposals, "p1")
        assert row["execution_plan"] == "background CC, ~$0.50"

    async def test_recurring_stored(self, db_with_proposals):
        await ego_crud.create_proposal(
            db_with_proposals,
            **_make_proposal_kwargs("p1", recurring=True),
        )
        row = await ego_crud.get_proposal(db_with_proposals, "p1")
        assert row["recurring"] == 1

    async def test_defaults_for_new_fields(self, db_with_proposals):
        await ego_crud.create_proposal(
            db_with_proposals,
            **_make_proposal_kwargs("p1"),
        )
        row = await ego_crud.get_proposal(db_with_proposals, "p1")
        assert row["rank"] is None
        assert row["execution_plan"] is None
        assert row["recurring"] == 0


class TestTableProposal:
    async def test_table_pending(self, db_with_proposals):
        await ego_crud.create_proposal(
            db_with_proposals, **_make_proposal_kwargs("p1"),
        )
        ok = await ego_crud.table_proposal(db_with_proposals, "p1")
        assert ok is True
        row = await ego_crud.get_proposal(db_with_proposals, "p1")
        assert row["status"] == "tabled"
        assert row["rank"] is None
        assert row["resolved_at"] is not None

    async def test_table_nonpending_fails(self, db_with_proposals):
        await ego_crud.create_proposal(
            db_with_proposals, **_make_proposal_kwargs("p1"),
        )
        await ego_crud.resolve_proposal(db_with_proposals, "p1", status="approved")
        ok = await ego_crud.table_proposal(db_with_proposals, "p1")
        assert ok is False

    async def test_table_nonexistent(self, db_with_proposals):
        ok = await ego_crud.table_proposal(db_with_proposals, "nope")
        assert ok is False


class TestWithdrawProposal:
    async def test_withdraw_pending(self, db_with_proposals):
        await ego_crud.create_proposal(
            db_with_proposals, **_make_proposal_kwargs("p1"),
        )
        ok = await ego_crud.withdraw_proposal(db_with_proposals, "p1")
        assert ok is True
        row = await ego_crud.get_proposal(db_with_proposals, "p1")
        assert row["status"] == "withdrawn"
        assert row["rank"] is None
        assert row["resolved_at"] is not None

    async def test_withdraw_nonpending_fails(self, db_with_proposals):
        await ego_crud.create_proposal(
            db_with_proposals, **_make_proposal_kwargs("p1"),
        )
        await ego_crud.resolve_proposal(db_with_proposals, "p1", status="rejected")
        ok = await ego_crud.withdraw_proposal(db_with_proposals, "p1")
        assert ok is False


class TestGetBoard:
    async def test_empty_board(self, db_with_proposals):
        board = await ego_crud.get_board(db_with_proposals)
        assert board == []

    async def test_board_returns_pending_only(self, db_with_proposals):
        await ego_crud.create_proposal(
            db_with_proposals, **_make_proposal_kwargs("p1"),
        )
        await ego_crud.create_proposal(
            db_with_proposals, **_make_proposal_kwargs("p2"),
        )
        await ego_crud.resolve_proposal(db_with_proposals, "p2", status="approved")
        board = await ego_crud.get_board(db_with_proposals)
        assert len(board) == 1
        assert board[0]["id"] == "p1"

    async def test_board_respects_size(self, db_with_proposals):
        for i in range(5):
            await ego_crud.create_proposal(
                db_with_proposals,
                **_make_proposal_kwargs(f"p{i}", created_at=f"2026-04-2{i}T10:00:00Z"),
            )
        board = await ego_crud.get_board(db_with_proposals, board_size=3)
        assert len(board) == 3

    async def test_board_ordered_by_rank(self, db_with_proposals):
        await ego_crud.create_proposal(
            db_with_proposals, **_make_proposal_kwargs("p1", rank=3),
        )
        await ego_crud.create_proposal(
            db_with_proposals, **_make_proposal_kwargs("p2", rank=1),
        )
        await ego_crud.create_proposal(
            db_with_proposals, **_make_proposal_kwargs("p3", rank=2),
        )
        board = await ego_crud.get_board(db_with_proposals, board_size=3)
        assert [b["id"] for b in board] == ["p2", "p3", "p1"]

    async def test_board_nulls_last(self, db_with_proposals):
        await ego_crud.create_proposal(
            db_with_proposals,
            **_make_proposal_kwargs("p1", created_at="2026-04-21T10:00:00Z"),
        )
        await ego_crud.create_proposal(
            db_with_proposals, **_make_proposal_kwargs("p2", rank=1, created_at="2026-04-20T10:00:00Z"),
        )
        board = await ego_crud.get_board(db_with_proposals, board_size=5)
        assert board[0]["id"] == "p2"  # ranked first
        assert board[1]["id"] == "p1"  # unranked last


class TestGetTabled:
    async def test_empty(self, db_with_proposals):
        assert await ego_crud.get_tabled(db_with_proposals) == []

    async def test_returns_tabled_only(self, db_with_proposals):
        await ego_crud.create_proposal(
            db_with_proposals, **_make_proposal_kwargs("p1"),
        )
        await ego_crud.create_proposal(
            db_with_proposals, **_make_proposal_kwargs("p2"),
        )
        await ego_crud.table_proposal(db_with_proposals, "p1")
        tabled = await ego_crud.get_tabled(db_with_proposals)
        assert len(tabled) == 1
        assert tabled[0]["id"] == "p1"


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
