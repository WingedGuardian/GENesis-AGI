"""Tests for message_queue CRUD operations."""

import pytest

from genesis.db.crud import message_queue


@pytest.fixture
def msg_fields():
    return dict(
        id="mq-1",
        source="cc_background",
        target="user",
        message_type="question",
        priority="high",
        content='{"text":"Which vehicle?","options":["Civic","RAV4"]}',
        session_id="sess-abc",
        created_at="2026-03-07T12:00:00",
    )


async def test_create_and_get(db, msg_fields):
    await message_queue.create(db, **msg_fields)
    row = await message_queue.get_by_id(db, "mq-1")
    assert row is not None
    assert row["source"] == "cc_background"
    assert row["message_type"] == "question"


async def test_query_pending(db, msg_fields):
    await message_queue.create(db, **msg_fields)
    await message_queue.create(db, **{**msg_fields, "id": "mq-2", "target": "az"})
    rows = await message_queue.query_pending(db, target="user")
    assert len(rows) == 1
    assert rows[0]["id"] == "mq-1"


async def test_set_response(db, msg_fields):
    await message_queue.create(db, **msg_fields)
    ok = await message_queue.set_response(
        db, "mq-1", response='{"choice":1}', responded_at="2026-03-07T12:05:00",
    )
    assert ok
    row = await message_queue.get_by_id(db, "mq-1")
    assert row["response"] == '{"choice":1}'
    assert row["responded_at"] == "2026-03-07T12:05:00"


async def test_query_pending_excludes_responded(db, msg_fields):
    await message_queue.create(db, **msg_fields)
    await message_queue.set_response(
        db, "mq-1", response='{"choice":1}', responded_at="2026-03-07T12:05:00",
    )
    rows = await message_queue.query_pending(db, target="user")
    assert len(rows) == 0


async def test_query_by_session(db, msg_fields):
    await message_queue.create(db, **msg_fields)
    await message_queue.create(db, **{**msg_fields, "id": "mq-2", "session_id": "sess-other"})
    rows = await message_queue.query_by_session(db, "sess-abc")
    assert len(rows) == 1


async def test_set_expired(db, msg_fields):
    await message_queue.create(db, **msg_fields)
    ok = await message_queue.set_expired(db, "mq-1", expired_at="2026-03-10T12:00:00")
    assert ok
    row = await message_queue.get_by_id(db, "mq-1")
    assert row["expired_at"] == "2026-03-10T12:00:00"


async def test_count_pending(db, msg_fields):
    await message_queue.create(db, **msg_fields)
    await message_queue.create(
        db, **{**msg_fields, "id": "mq-2", "target": "cc_foreground"},
    )
    assert await message_queue.count_pending(db, target="user") == 1
    assert await message_queue.count_pending(db) == 2


async def test_delete(db, msg_fields):
    await message_queue.create(db, **msg_fields)
    assert await message_queue.delete(db, "mq-1")
    assert await message_queue.get_by_id(db, "mq-1") is None


async def test_check_constraint_message_type(db, msg_fields):
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        await message_queue.create(db, **{**msg_fields, "message_type": "invalid"})


async def test_check_constraint_priority(db, msg_fields):
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        await message_queue.create(db, **{**msg_fields, "priority": "ultra"})


async def test_expire_older_than(db, msg_fields):
    """Messages older than max_age_hours get expired; recent ones survive."""
    # Create an old message (created 10 days ago)
    await message_queue.create(
        db, **{**msg_fields, "id": "mq-old", "created_at": "2026-03-01T00:00:00"},
    )
    # Create a recent message
    await message_queue.create(
        db, **{**msg_fields, "id": "mq-recent", "created_at": "2026-03-27T00:00:00"},
    )
    expired = await message_queue.expire_older_than(
        db, max_age_hours=168, expired_at="2026-03-27T12:00:00",
    )
    assert expired == 1
    old = await message_queue.get_by_id(db, "mq-old")
    assert old["expired_at"] == "2026-03-27T12:00:00"
    recent = await message_queue.get_by_id(db, "mq-recent")
    assert recent["expired_at"] is None


async def test_expire_older_than_skips_responded(db, msg_fields):
    """Already-responded messages should not be expired."""
    await message_queue.create(
        db, **{**msg_fields, "id": "mq-responded", "created_at": "2026-03-01T00:00:00"},
    )
    await message_queue.set_response(
        db, "mq-responded", response="done", responded_at="2026-03-02T00:00:00",
    )
    expired = await message_queue.expire_older_than(
        db, max_age_hours=168, expired_at="2026-03-27T12:00:00",
    )
    assert expired == 0
