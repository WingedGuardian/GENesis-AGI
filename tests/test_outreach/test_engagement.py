"""Tests for engagement tracking."""

from datetime import UTC, datetime, timedelta

import aiosqlite
import pytest

from genesis.db.crud import outreach as outreach_crud
from genesis.db.schema import create_all_tables
from genesis.outreach.engagement import EngagementTracker


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await create_all_tables(conn)
    yield conn
    await conn.close()


@pytest.mark.asyncio
async def test_check_timeouts_marks_ignored(db):
    old = (datetime.now(UTC) - timedelta(hours=48)).isoformat()
    await outreach_crud.create(
        db, id="old-1", signal_type="surplus", topic="Old",
        category="surplus", salience_score=0.8, channel="telegram",
        message_content="Hello", created_at=old,
    )
    await outreach_crud.record_delivery(db, "old-1", delivered_at=old)

    tracker = EngagementTracker(db)
    count = await tracker.check_timeouts(timeout_hours=24)
    assert count == 1

    row = await outreach_crud.get_by_id(db, "old-1")
    assert row["engagement_outcome"] == "ignored"


@pytest.mark.asyncio
async def test_check_timeouts_skips_recent(db):
    now = datetime.now(UTC).isoformat()
    await outreach_crud.create(
        db, id="recent-1", signal_type="surplus", topic="Recent",
        category="surplus", salience_score=0.8, channel="telegram",
        message_content="Hello", created_at=now,
    )
    await outreach_crud.record_delivery(db, "recent-1", delivered_at=now)

    tracker = EngagementTracker(db)
    count = await tracker.check_timeouts(timeout_hours=24)
    assert count == 0


@pytest.mark.asyncio
async def test_check_timeouts_skips_already_engaged(db):
    old = (datetime.now(UTC) - timedelta(hours=48)).isoformat()
    await outreach_crud.create(
        db, id="engaged-1", signal_type="surplus", topic="Engaged",
        category="surplus", salience_score=0.8, channel="telegram",
        message_content="Hello", created_at=old,
    )
    await outreach_crud.record_delivery(db, "engaged-1", delivered_at=old)
    await outreach_crud.record_engagement(db, "engaged-1", engagement_outcome="engaged", engagement_signal="replied")

    tracker = EngagementTracker(db)
    count = await tracker.check_timeouts(timeout_hours=24)
    assert count == 0


@pytest.mark.asyncio
async def test_find_outreach_for_reply(db):
    now = datetime.now(UTC).isoformat()
    await outreach_crud.create(
        db, id="match-1", signal_type="surplus", topic="Match",
        category="surplus", salience_score=0.8, channel="telegram",
        message_content="Hello", created_at=now, delivery_id="tg-999",
    )
    await outreach_crud.record_delivery(db, "match-1", delivered_at=now)

    tracker = EngagementTracker(db)
    outreach_id = await tracker.find_outreach_for_reply("tg-999")
    assert outreach_id == "match-1"


@pytest.mark.asyncio
async def test_record_reply(db):
    now = datetime.now(UTC).isoformat()
    await outreach_crud.create(
        db, id="reply-1", signal_type="surplus", topic="Reply test",
        category="surplus", salience_score=0.8, channel="telegram",
        message_content="Hello", created_at=now, delivery_id="tg-888",
    )
    await outreach_crud.record_delivery(db, "reply-1", delivered_at=now)

    tracker = EngagementTracker(db)
    result = await tracker.record_reply("reply-1", "Thanks, that's helpful!")
    assert result is True

    row = await outreach_crud.get_by_id(db, "reply-1")
    assert row["engagement_outcome"] == "useful"
    assert row["user_response"] == "Thanks, that's helpful!"
