"""PATH A wiring: outreach pipeline._deliver observes Discord sends (shadow) yet still sends."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest

from genesis.content.types import FormatTarget, FormattedContent
from genesis.db.crud import capability_shadow
from genesis.db.schema import create_all_tables
from genesis.outreach.config import OutreachConfig, QuietHours
from genesis.outreach.governance import GovernanceGate
from genesis.outreach.pipeline import OutreachPipeline
from genesis.outreach.types import OutreachCategory, OutreachRequest, OutreachStatus


@pytest.fixture(autouse=True)
def _reset_table_cache():
    capability_shadow._table_verified = False
    yield
    capability_shadow._table_verified = False


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await create_all_tables(conn)
    yield conn
    await conn.close()


def _config():
    return OutreachConfig(
        quiet_hours=QuietHours(start="22:00", end="07:00"),
        channel_preferences={"default": "discord"},
        thresholds={"blocker": 0.0, "alert": 0.3, "surplus": 0.7, "digest": 0.0},
        max_daily=5, surplus_daily=1, content_daily=3, notification_daily=10,
        morning_report_time="07:00", engagement_timeout_hours=24, engagement_poll_minutes=60,
    )


def _pipeline(cfg, db, channel):
    return OutreachPipeline(
        governance=GovernanceGate(cfg, db), drafter=AsyncMock(), formatter=MagicMock(),
        channels={"discord": channel}, db=db, config=cfg,
        recipients={"discord": "announcements"},
    )


def _req(recipient="announcements"):
    return OutreachRequest(
        category=OutreachCategory.SURPLUS, topic="hi", context="ctx",
        salience_score=0.9, signal_type="surplus_insight", channel="discord",
        validated_recipient=recipient,
    )


@pytest.mark.asyncio
async def test_deliver_discord_records_shadow_and_still_sends(db):
    channel = AsyncMock()
    channel.send_message.return_value = "disc-1"
    pipeline = _pipeline(_config(), db, channel)
    formatted = FormattedContent(text="hello discord community", target=FormatTarget.TELEGRAM)

    result = await pipeline._deliver("oid-1", "discord", formatted, _req(), None)

    assert result.status == OutreachStatus.DELIVERED
    channel.send_message.assert_called_once()  # the real send still happened
    rows = await capability_shadow.list_recent(db)
    assert len(rows) == 1
    r = rows[0]
    assert r["path"] == "deliver" and r["cell_domain"] == "discord"
    assert r["cell_verb"] == "send" and r["cell_risk_class"] == "bulk"
    assert r["would_hold"] == 1  # no discord cell => not_determined => would hold
    assert r["target"] == "announcements"


@pytest.mark.asyncio
async def test_deliver_gate_cleared_skips_shadow(db):
    # The below-the-gate resume path (gate_cleared=True) must NOT be observed.
    channel = AsyncMock()
    channel.send_message.return_value = "disc-2"
    pipeline = _pipeline(_config(), db, channel)
    formatted = FormattedContent(text="resend", target=FormatTarget.TELEGRAM)

    await pipeline._deliver("oid-2", "discord", formatted, _req("general"), None, gate_cleared=True)

    channel.send_message.assert_called_once()
    assert await capability_shadow.count(db) == 0
