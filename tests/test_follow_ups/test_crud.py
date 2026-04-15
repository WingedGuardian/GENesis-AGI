"""Tests for follow_ups CRUD operations."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import aiosqlite
import pytest

from genesis.db.crud import follow_ups
from genesis.db.schema import INDEXES, TABLES


@pytest.fixture
async def db():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute(TABLES["follow_ups"])
        for idx in INDEXES:
            if "follow_ups" in idx:
                await conn.execute(idx)
        yield conn


class TestFollowUpCRUD:
    async def test_create_and_get(self, db):
        fid = await follow_ups.create(
            db,
            content="Run Gemini benchmark",
            source="foreground_session",
            strategy="surplus_task",
            reason="Daily quota reset",
        )
        assert fid

        fu = await follow_ups.get_by_id(db, fid)
        assert fu is not None
        assert fu["content"] == "Run Gemini benchmark"
        assert fu["status"] == "pending"
        assert fu["strategy"] == "surplus_task"
        assert fu["source"] == "foreground_session"

    async def test_get_pending_filters(self, db):
        await follow_ups.create(db, content="A", source="ego", strategy="ego_judgment")
        await follow_ups.create(db, content="B", source="session", strategy="surplus_task")
        await follow_ups.create(db, content="C", source="ego", strategy="surplus_task")

        all_pending = await follow_ups.get_pending(db)
        assert len(all_pending) == 3

        ego_only = await follow_ups.get_pending(db, source="ego")
        assert len(ego_only) == 2

        surplus_only = await follow_ups.get_pending(db, strategy="surplus_task")
        assert len(surplus_only) == 2

    async def test_update_status_sets_completed_at(self, db):
        fid = await follow_ups.create(db, content="X", source="test", strategy="surplus_task")
        await follow_ups.update_status(db, fid, "completed", resolution_notes="done")

        fu = await follow_ups.get_by_id(db, fid)
        assert fu["status"] == "completed"
        assert fu["completed_at"] is not None
        assert fu["resolution_notes"] == "done"

    async def test_link_task(self, db):
        fid = await follow_ups.create(db, content="X", source="test", strategy="surplus_task")
        await follow_ups.link_task(db, fid, "task-123")

        fu = await follow_ups.get_by_id(db, fid)
        assert fu["linked_task_id"] == "task-123"
        assert fu["status"] == "scheduled"

    async def test_get_actionable_respects_limit(self, db):
        for i in range(60):
            await follow_ups.create(db, content=f"item-{i}", source="test", strategy="ego_judgment")

        actionable = await follow_ups.get_actionable(db, limit=10)
        assert len(actionable) == 10

    async def test_get_scheduled_due(self, db):
        past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()

        await follow_ups.create(
            db, content="past", source="test",
            strategy="scheduled_task", scheduled_at=past,
        )
        await follow_ups.create(
            db, content="future", source="test",
            strategy="scheduled_task", scheduled_at=future,
        )

        due = await follow_ups.get_scheduled_due(db)
        assert len(due) == 1
        assert due[0]["content"] == "past"

    async def test_get_linked_active(self, db):
        fid = await follow_ups.create(db, content="X", source="test", strategy="surplus_task")
        await follow_ups.link_task(db, fid, "task-abc")

        linked = await follow_ups.get_linked_active(db)
        assert len(linked) == 1
        assert linked[0]["linked_task_id"] == "task-abc"

    async def test_summary_counts(self, db):
        await follow_ups.create(db, content="A", source="t", strategy="surplus_task")
        await follow_ups.create(db, content="B", source="t", strategy="surplus_task")
        fid = await follow_ups.create(db, content="C", source="t", strategy="surplus_task")
        await follow_ups.update_status(db, fid, "completed")

        counts = await follow_ups.get_summary_counts(db)
        assert counts.get("pending", 0) == 2
        assert counts.get("completed", 0) == 1

    async def test_escalate(self, db):
        fid = await follow_ups.create(db, content="X", source="test", strategy="ego_judgment")
        await follow_ups.escalate(db, fid, "task:t-123")

        fu = await follow_ups.get_by_id(db, fid)
        assert fu["escalated_to"] == "task:t-123"
