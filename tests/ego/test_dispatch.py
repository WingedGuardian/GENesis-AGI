"""Tests for the ego follow-up dispatcher."""

from __future__ import annotations

import aiosqlite
import pytest

from genesis.db.schema import TABLES
from genesis.ego.dispatch import EgoDispatcher


@pytest.fixture
async def db():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute(TABLES["ego_state"])
        yield conn


@pytest.fixture
def dispatcher(db):
    return EgoDispatcher(db=db)


class TestEgoDispatcher:
    async def test_record_and_get_roundtrip(self, dispatcher):
        count = await dispatcher.record_follow_ups(
            ["investigate backlog", "check CC bridge"], cycle_id="c1",
        )
        assert count == 2

        pending = await dispatcher.get_pending_follow_ups()
        assert len(pending) == 2
        texts = {p["text"] for p in pending}
        assert texts == {"investigate backlog", "check CC bridge"}
        assert all(p["cycle_id"] == "c1" for p in pending)
        assert all("key" in p for p in pending)

    async def test_empty_follow_ups(self, dispatcher):
        assert await dispatcher.get_pending_follow_ups() == []

    async def test_skips_empty_strings(self, dispatcher):
        count = await dispatcher.record_follow_ups(
            ["real task", "", "  ", "another task"], cycle_id="c1",
        )
        assert count == 2
        assert len(await dispatcher.get_pending_follow_ups()) == 2

    async def test_clear_follow_up(self, dispatcher):
        await dispatcher.record_follow_ups(["task1", "task2"], cycle_id="c1")
        pending = await dispatcher.get_pending_follow_ups()
        assert len(pending) == 2

        await dispatcher.clear_follow_up(pending[0]["key"])
        remaining = await dispatcher.get_pending_follow_ups()
        assert len(remaining) == 1
        assert remaining[0]["text"] == pending[1]["text"]

    async def test_new_cycle_replaces_old_follow_ups(self, dispatcher):
        """Each record_follow_ups call clears prior follow_ups to prevent unbounded growth."""
        await dispatcher.record_follow_ups(["task A"], cycle_id="c1")
        assert len(await dispatcher.get_pending_follow_ups()) == 1

        await dispatcher.record_follow_ups(["task B", "task C"], cycle_id="c2")
        pending = await dispatcher.get_pending_follow_ups()
        assert len(pending) == 2
        assert all(p["cycle_id"] == "c2" for p in pending)

    async def test_clear_nonexistent_key(self, dispatcher):
        # Should not raise
        await dispatcher.clear_follow_up("follow_up:nonexistent")
        assert await dispatcher.get_pending_follow_ups() == []
