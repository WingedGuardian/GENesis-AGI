"""Tests for the real OutreachEngagementCollector."""

from datetime import UTC, datetime

import aiosqlite
import pytest

from genesis.db.crud import outreach as outreach_crud
from genesis.db.schema import create_all_tables
from genesis.learning.signals.outreach_engagement import OutreachEngagementCollector


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await create_all_tables(conn)
    yield conn
    await conn.close()


@pytest.mark.asyncio
async def test_no_outreach_returns_zero(db):
    collector = OutreachEngagementCollector(db)
    reading = await collector.collect()
    assert reading.name == "outreach_engagement_data"
    assert reading.value == 0.0


@pytest.mark.asyncio
async def test_all_engaged_returns_high(db):
    now = datetime.now(UTC).isoformat()
    for i in range(3):
        await outreach_crud.create(
            db, id=f"e-{i}", signal_type="surplus", topic=f"T{i}",
            category="surplus", salience_score=0.8, channel="telegram",
            message_content="Hi", created_at=now,
        )
        await outreach_crud.record_delivery(db, f"e-{i}", delivered_at=now)
        await outreach_crud.record_engagement(db, f"e-{i}", engagement_outcome="engaged", engagement_signal="reply")

    collector = OutreachEngagementCollector(db)
    reading = await collector.collect()
    assert reading.value == 1.0


@pytest.mark.asyncio
async def test_mixed_engagement(db):
    now = datetime.now(UTC).isoformat()
    for i in range(2):
        await outreach_crud.create(
            db, id=f"eng-{i}", signal_type="surplus", topic=f"E{i}",
            category="surplus", salience_score=0.8, channel="telegram",
            message_content="Hi", created_at=now,
        )
        await outreach_crud.record_delivery(db, f"eng-{i}", delivered_at=now)
        await outreach_crud.record_engagement(db, f"eng-{i}", engagement_outcome="engaged", engagement_signal="reply")
    for i in range(2):
        await outreach_crud.create(
            db, id=f"ign-{i}", signal_type="surplus", topic=f"I{i}",
            category="surplus", salience_score=0.8, channel="telegram",
            message_content="Hi", created_at=now,
        )
        await outreach_crud.record_delivery(db, f"ign-{i}", delivered_at=now)
        await outreach_crud.record_engagement(db, f"ign-{i}", engagement_outcome="ignored", engagement_signal="timeout")

    collector = OutreachEngagementCollector(db)
    reading = await collector.collect()
    assert reading.value == pytest.approx(0.5)
