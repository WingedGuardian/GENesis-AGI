"""Tests for the ego follow-up dispatcher (follow_ups table backend)."""

from __future__ import annotations

import aiosqlite
import pytest

from genesis.db.schema import INDEXES, TABLES
from genesis.ego.dispatch import EgoDispatcher


@pytest.fixture
async def db():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute(TABLES["follow_ups"])
        for idx in INDEXES:
            if "follow_ups" in idx:
                await conn.execute(idx)
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
        contents = {p["content"] for p in pending}
        assert contents == {"investigate backlog", "check CC bridge"}

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

        await dispatcher.clear_follow_up(pending[0]["id"])
        remaining = await dispatcher.get_pending_follow_ups()
        assert len(remaining) == 1

    async def test_follow_ups_accumulate(self, dispatcher):
        """Follow-ups persist across cycles (no clearing on new record)."""
        await dispatcher.record_follow_ups(["task A"], cycle_id="c1")
        assert len(await dispatcher.get_pending_follow_ups()) == 1

        await dispatcher.record_follow_ups(["task B", "task C"], cycle_id="c2")
        pending = await dispatcher.get_pending_follow_ups()
        # All 3 follow-ups should be present (not just the latest cycle's)
        assert len(pending) == 3

    async def test_clear_nonexistent_id(self, dispatcher):
        # Should not raise — update_status on nonexistent ID just does nothing
        await dispatcher.clear_follow_up("nonexistent-id")
        assert await dispatcher.get_pending_follow_ups() == []

    async def test_dedup_same_content(self, dispatcher):
        """Duplicate content from a second cycle is NOT re-recorded."""
        await dispatcher.record_follow_ups(["investigate backlog"], cycle_id="c1")
        count = await dispatcher.record_follow_ups(
            ["investigate backlog"], cycle_id="c2",
        )
        assert count == 0  # duplicate skipped
        assert len(await dispatcher.get_pending_follow_ups()) == 1

    async def test_dedup_case_insensitive(self, dispatcher):
        """Dedup is case-insensitive."""
        await dispatcher.record_follow_ups(["Check backlog"], cycle_id="c1")
        count = await dispatcher.record_follow_ups(
            ["check backlog"], cycle_id="c2",
        )
        assert count == 0
        assert len(await dispatcher.get_pending_follow_ups()) == 1

    async def test_dedup_within_single_batch(self, dispatcher):
        """Duplicate entries within one cycle are also deduped."""
        count = await dispatcher.record_follow_ups(
            ["same task", "same task"], cycle_id="c1",
        )
        assert count == 1
        assert len(await dispatcher.get_pending_follow_ups()) == 1

    async def test_dedup_allows_different_content(self, dispatcher):
        """Non-duplicate content from a second cycle IS recorded."""
        await dispatcher.record_follow_ups(["task A"], cycle_id="c1")
        count = await dispatcher.record_follow_ups(
            ["task B"], cycle_id="c2",
        )
        assert count == 1
        assert len(await dispatcher.get_pending_follow_ups()) == 2

    async def test_resolve_follow_ups(self, dispatcher):
        """Resolve a follow-up by ID."""
        await dispatcher.record_follow_ups(["task to resolve"], cycle_id="c1")
        pending = await dispatcher.get_pending_follow_ups()
        assert len(pending) == 1
        fid = pending[0]["id"]

        count = await dispatcher.resolve_follow_ups(
            [{"id": fid, "resolution": "Done by ego"}], cycle_id="c2",
        )
        assert count == 1
        assert len(await dispatcher.get_pending_follow_ups()) == 0

    async def test_resolve_nonexistent_follow_up(self, dispatcher):
        """Resolving a nonexistent follow-up returns 0, no error."""
        count = await dispatcher.resolve_follow_ups(
            [{"id": "nonexistent", "resolution": "n/a"}], cycle_id="c1",
        )
        assert count == 0

    async def test_resolve_skips_invalid_items(self, dispatcher):
        """Invalid items in resolved list are skipped."""
        count = await dispatcher.resolve_follow_ups(
            ["not a dict", {"no_id": True}, {"id": "", "resolution": "test"}],
            cycle_id="c1",
        )
        assert count == 0
