"""Tests for outreach dashboard API endpoints."""

from datetime import UTC, datetime

import aiosqlite
import pytest

from genesis.db.crud import outreach as outreach_crud
from genesis.db.schema import create_all_tables
from genesis.outreach.api import engagement_summary_7d


def test_blueprint_importable():
    from genesis.outreach.api import outreach_api
    assert outreach_api.name == "outreach_api"


def test_blueprint_has_url_prefix():
    from genesis.outreach.api import outreach_api
    assert outreach_api.url_prefix == "/api/genesis/outreach"


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
        db, id=rid, signal_type=category, topic=rid, category=category,
        salience_score=0.8, channel=channel, message_content="x", created_at=now,
    )
    await outreach_crud.record_delivery(db, rid, delivered_at=now)
    await outreach_crud.record_engagement(
        db, rid, engagement_outcome=outcome, engagement_signal=signal,
    )


@pytest.mark.asyncio
async def test_engagement_summary_excludes_owner_channel(db):
    """The /engagement API route must apply the same channel filter as the
    other engagement sites — owner-facing Telegram housekeeping must not
    pollute the rate; genuine external (discord/email) counts."""
    await _row(db, "c-0", "content", "discord", outcome="useful")
    await _row(db, "mail-0", "notification", "email", outcome="useful")
    for i in range(4):
        await _row(db, f"appr-{i}", "approval", "telegram", outcome="ignored", signal="timeout")

    summary = await engagement_summary_7d(db)
    assert summary["total"] == 2  # only the two external rows
    assert summary["engagement_rate"] == 1.0  # was 2/6 ≈ 0.33 when relay polluted it
