"""Tests for the outreach_stats health snapshot — owner-facing relay traffic
(delivered over Telegram) must not inflate the 'N sent' total or deflate the
engagement rate; genuine external touches (discord/email) must count."""

from datetime import UTC, datetime

import aiosqlite
import pytest

from genesis.db.crud import outreach as outreach_crud
from genesis.db.schema import create_all_tables
from genesis.observability.snapshots.outreach import outreach_stats


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await create_all_tables(conn)
    yield conn
    await conn.close()


async def _row(db, rid, category, channel, *, outcome, signal="user_reply"):
    now = datetime.now(UTC).isoformat()
    await outreach_crud.create(
        db,
        id=rid,
        signal_type=category,
        topic=rid,
        category=category,
        salience_score=0.8,
        channel=channel,
        message_content="x",
        created_at=now,
    )
    await outreach_crud.record_delivery(db, rid, delivered_at=now)
    if outcome is not None:
        await outreach_crud.record_engagement(
            db,
            rid,
            engagement_outcome=outcome,
            engagement_signal=signal,
        )


@pytest.mark.asyncio
async def test_no_db_returns_unknown():
    assert await outreach_stats(None) == {"status": "unknown"}


@pytest.mark.asyncio
async def test_owner_channel_excluded_external_counted(db):
    # Genuine external touches (non-Telegram) that got real replies...
    await _row(db, "c-0", "content", "discord", outcome="useful")
    await _row(db, "mail-0", "notification", "email", outcome="useful")
    # ...and a pile of owner-facing Telegram housekeeping that must NOT count.
    for i in range(5):
        await _row(db, f"appr-{i}", "approval", "telegram", outcome="ignored", signal="timeout")
    await _row(db, "dig-0", "digest", "telegram", outcome=None)
    await _row(db, "blk-0", "blocker", "telegram", outcome="ignored", signal="timeout")

    stats = await outreach_stats(db)
    # Only the two external rows are genuine outreach.
    assert stats["total"] == 2
    assert stats["delivery_rate"] == 1.0
    assert stats["engagement_rate"] == 1.0  # was ~0.15 when relay polluted the denominator
