"""Tests for the ego follow-up dispatcher (follow_ups table backend).

Note: record_follow_ups() is intentionally a no-op (PR #375) — ego-generated
follow-ups had an 84% stale rate. Ego can still READ and RESOLVE follow-ups
created by foreground sessions.
"""

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
    async def test_record_is_noop(self, dispatcher):
        """record_follow_ups always returns 0 — creation is disabled."""
        count = await dispatcher.record_follow_ups(
            ["investigate backlog", "check CC bridge"], cycle_id="c1",
        )
        assert count == 0
        assert await dispatcher.get_pending_follow_ups() == []

    async def test_record_noop_with_empty_strings(self, dispatcher):
        """No-op regardless of input content (empty strings, whitespace)."""
        count = await dispatcher.record_follow_ups(
            ["real task", "", "  ", "another task"], cycle_id="c1",
        )
        assert count == 0

    async def test_record_noop_multiple_cycles(self, dispatcher):
        """Multiple record calls all return 0."""
        assert await dispatcher.record_follow_ups(["A"], cycle_id="c1") == 0
        assert await dispatcher.record_follow_ups(["B", "C"], cycle_id="c2") == 0
        assert await dispatcher.get_pending_follow_ups() == []

    async def test_empty_follow_ups(self, dispatcher):
        assert await dispatcher.get_pending_follow_ups() == []

    async def test_clear_nonexistent_id(self, dispatcher):
        """Clear on nonexistent ID does not raise."""
        await dispatcher.clear_follow_up("nonexistent-id")
        assert await dispatcher.get_pending_follow_ups() == []

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

    async def test_get_pending_reads_existing_follow_ups(self, db, dispatcher):
        """get_pending_follow_ups reads from DB even though record is disabled."""
        # Insert a follow-up directly (simulates foreground session creation)
        import uuid
        from datetime import UTC, datetime

        fid = str(uuid.uuid4()).replace("-", "")
        await db.execute(
            "INSERT INTO follow_ups (id, source, content, reason, strategy, status, priority, created_at) "
            "VALUES (?, 'foreground_session', 'test follow-up', 'test', 'ego_judgment', 'pending', 'medium', ?)",
            (fid, datetime.now(UTC).isoformat()),
        )
        await db.commit()

        pending = await dispatcher.get_pending_follow_ups()
        assert len(pending) >= 1
        assert any(p["content"] == "test follow-up" for p in pending)

    async def test_resolve_existing_follow_up(self, db, dispatcher):
        """Ego can resolve follow-ups created by other sources."""
        import uuid
        from datetime import UTC, datetime

        fid = str(uuid.uuid4()).replace("-", "")
        await db.execute(
            "INSERT INTO follow_ups (id, source, content, reason, strategy, status, priority, created_at) "
            "VALUES (?, 'foreground_session', 'task to resolve', 'test', 'ego_judgment', 'pending', 'medium', ?)",
            (fid, datetime.now(UTC).isoformat()),
        )
        await db.commit()

        count = await dispatcher.resolve_follow_ups(
            [{"id": fid, "resolution": "Done by ego"}], cycle_id="c1",
        )
        assert count == 1
