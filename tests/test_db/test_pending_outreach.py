"""CRUD tests for pending_outreach — thread_id / validated_recipient round-trip.

The subprocess fallback path (``outreach_send`` with ``pipeline=None``) enqueues
here. It must persist the resolved thread + recipient so the genesis-server
drain can rebuild a properly-routed request instead of defaulting a recipient-
less email to the agent's own address.
"""

from __future__ import annotations

import aiosqlite
import pytest

from genesis.db.crud import pending_outreach


@pytest.fixture
async def db(tmp_path):
    async with aiosqlite.connect(str(tmp_path / "t.db")) as conn:
        conn.row_factory = aiosqlite.Row
        await pending_outreach.ensure_table(conn)
        yield conn


@pytest.mark.asyncio
async def test_ensure_table_includes_new_columns(db):
    cur = await db.execute("PRAGMA table_info(pending_outreach)")
    cols = {row[1] for row in await cur.fetchall()}
    assert "thread_id" in cols
    assert "validated_recipient" in cols


@pytest.mark.asyncio
async def test_enqueue_persists_thread_and_recipient(db):
    await pending_outreach.enqueue(
        db,
        message="follow up",
        category="notification",
        channel="email",
        thread_id="thread-123",
        validated_recipient="real@prospect.com",
    )
    rows = await pending_outreach.drain(db, now="2999-01-01T00:00:00+00:00")
    assert len(rows) == 1
    assert rows[0]["thread_id"] == "thread-123"
    assert rows[0]["validated_recipient"] == "real@prospect.com"


@pytest.mark.asyncio
async def test_enqueue_defaults_to_none(db):
    await pending_outreach.enqueue(
        db, message="m", category="notification", channel="telegram",
    )
    rows = await pending_outreach.drain(db, now="2999-01-01T00:00:00+00:00")
    assert len(rows) == 1
    assert rows[0]["thread_id"] is None
    assert rows[0]["validated_recipient"] is None
