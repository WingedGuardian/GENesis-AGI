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
        db,
        message="m",
        category="notification",
        channel="telegram",
    )
    rows = await pending_outreach.drain(db, now="2999-01-01T00:00:00+00:00")
    assert len(rows) == 1
    assert rows[0]["thread_id"] is None
    assert rows[0]["validated_recipient"] is None


@pytest.mark.asyncio
async def test_drain_exposes_rowid(db):
    """drain must surface rowid so NULL-id rows can be cleared by rowid."""
    await pending_outreach.enqueue(
        db,
        message="m",
        category="notification",
        channel="telegram",
    )
    rows = await pending_outreach.drain(db, now="2999-01-01T00:00:00+00:00")
    assert len(rows) == 1
    assert isinstance(rows[0]["rowid"], int)


@pytest.mark.asyncio
async def test_mark_delivered_by_rowid_clears_null_id_row(db):
    """A NULL-id row can't be marked by id (WHERE id=NULL matches nothing);
    mark_delivered_by_rowid targets it by its always-present rowid."""
    await db.execute(
        "INSERT INTO pending_outreach (message, category, channel, urgency, "
        "created_at, delivered) VALUES ('m', 'notification', 'telegram', "
        "'low', '2020-01-01T00:00:00+00:00', 0)",
    )
    await db.commit()
    rows = await pending_outreach.drain(db, now="2999-01-01T00:00:00+00:00")
    assert len(rows) == 1 and rows[0]["id"] is None
    rowid = rows[0]["rowid"]

    # id-keyed mark is a no-op on a NULL id; rowid-keyed clears it.
    assert (
        await pending_outreach.mark_delivered(db, None, delivered_at="2026-01-01T00:00:00+00:00")
        is False
    )
    assert (
        await pending_outreach.mark_delivered_by_rowid(
            db, rowid, delivered_at="2026-01-01T00:00:00+00:00"
        )
        is True
    )
    assert await pending_outreach.drain(db, now="2999-01-01T00:00:00+00:00") == []
