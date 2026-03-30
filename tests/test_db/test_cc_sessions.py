"""Tests for cc_sessions CRUD operations."""

import pytest

from genesis.db.crud import cc_sessions


@pytest.fixture
def sess_fields():
    return dict(
        id="sess-1",
        session_type="foreground",
        model="sonnet",
        effort="medium",
        status="active",
        user_id="user-1",
        channel="telegram",
        started_at="2026-03-07T08:00:00",
        last_activity_at="2026-03-07T08:00:00",
    )


async def test_create_and_get(db, sess_fields):
    await cc_sessions.create(db, **sess_fields)
    row = await cc_sessions.get_by_id(db, "sess-1")
    assert row is not None
    assert row["session_type"] == "foreground"
    assert row["model"] == "sonnet"


async def test_get_active_foreground(db, sess_fields):
    await cc_sessions.create(db, **sess_fields)
    row = await cc_sessions.get_active_foreground(db, user_id="user-1", channel="telegram")
    assert row is not None
    assert row["id"] == "sess-1"


async def test_get_active_foreground_ignores_completed(db, sess_fields):
    await cc_sessions.create(db, **{**sess_fields, "status": "completed"})
    row = await cc_sessions.get_active_foreground(db, user_id="user-1", channel="telegram")
    assert row is None


async def test_update_status(db, sess_fields):
    await cc_sessions.create(db, **sess_fields)
    ok = await cc_sessions.update_status(db, "sess-1", status="checkpointed")
    assert ok
    row = await cc_sessions.get_by_id(db, "sess-1")
    assert row["status"] == "checkpointed"


async def test_update_activity(db, sess_fields):
    await cc_sessions.create(db, **sess_fields)
    ok = await cc_sessions.update_activity(
        db, "sess-1", last_activity_at="2026-03-07T09:00:00",
    )
    assert ok
    row = await cc_sessions.get_by_id(db, "sess-1")
    assert row["last_activity_at"] == "2026-03-07T09:00:00"


async def test_query_active(db, sess_fields):
    await cc_sessions.create(db, **sess_fields)
    await cc_sessions.create(db, **{**sess_fields, "id": "sess-2", "status": "completed"})
    rows = await cc_sessions.query_active(db)
    assert len(rows) == 1


async def test_query_stale(db, sess_fields):
    await cc_sessions.create(
        db, **{**sess_fields, "last_activity_at": "2026-03-07T06:00:00"},
    )
    rows = await cc_sessions.query_stale(db, older_than="2026-03-07T07:00:00")
    assert len(rows) == 1


async def test_delete(db, sess_fields):
    await cc_sessions.create(db, **sess_fields)
    assert await cc_sessions.delete(db, "sess-1")
    assert await cc_sessions.get_by_id(db, "sess-1") is None


async def test_check_constraint_session_type(db, sess_fields):
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        await cc_sessions.create(db, **{**sess_fields, "session_type": "invalid"})


async def test_check_constraint_status(db, sess_fields):
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        await cc_sessions.create(db, **{**sess_fields, "status": "invalid"})


@pytest.mark.asyncio
async def test_update_cc_session_id(db, sess_fields):
    await cc_sessions.create(db, **sess_fields)
    ok = await cc_sessions.update_cc_session_id(
        db, "sess-1", cc_session_id="cc-cli-uuid-123")
    assert ok
    row = await cc_sessions.get_by_id(db, "sess-1")
    assert row["cc_session_id"] == "cc-cli-uuid-123"


@pytest.mark.asyncio
async def test_cc_session_id_null_by_default(db, sess_fields):
    await cc_sessions.create(db, **sess_fields)
    row = await cc_sessions.get_by_id(db, "sess-1")
    assert row["cc_session_id"] is None
