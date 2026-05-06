"""Tests for outreach scheduler."""

from unittest.mock import AsyncMock, patch

import aiosqlite
import pytest

from genesis.db.schema import create_all_tables
from genesis.outreach.config import OutreachConfig, QuietHours
from genesis.outreach.scheduler import OutreachScheduler
from genesis.outreach.types import OutreachCategory, OutreachRequest, OutreachResult, OutreachStatus


@pytest.fixture
def config():
    return OutreachConfig(
        quiet_hours=QuietHours(start="22:00", end="07:00"),
        channel_preferences={"default": "telegram"},
        thresholds={"blocker": 0.0, "alert": 0.3, "surplus": 0.7, "digest": 0.0},
        max_daily=5, surplus_daily=1, content_daily=3,
        morning_report_time="07:00",
        engagement_timeout_hours=24, engagement_poll_minutes=60,
    )


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await create_all_tables(conn)
    yield conn
    await conn.close()


def test_scheduler_creates(config):
    pipeline = AsyncMock()
    morning = AsyncMock()
    engagement = AsyncMock()
    import unittest.mock
    mock_db = unittest.mock.MagicMock()
    scheduler = OutreachScheduler(pipeline, morning, engagement, config, mock_db)
    assert scheduler is not None


@pytest.mark.asyncio
async def test_surplus_job_picks_best_insight(config, db):
    await db.execute(
        "INSERT INTO surplus_insights (id, content, source_task_type, generating_model, "
        "drive_alignment, confidence, created_at, ttl, promotion_status) VALUES "
        "(?, ?, ?, ?, ?, ?, datetime('now'), datetime('now', '+24 hours'), 'pending')",
        ("si-1", "Great insight", "upgrade_user", "gemini", "cooperation", 0.9),
    )
    await db.commit()

    pipeline = AsyncMock()
    morning = AsyncMock()
    engagement = AsyncMock()
    scheduler = OutreachScheduler(pipeline, morning, engagement, config, db)

    await scheduler._surplus_outreach_job()
    pipeline.submit.assert_called_once()
    call_req = pipeline.submit.call_args[0][0]
    assert call_req.labeled_surplus is True


@pytest.mark.asyncio
async def test_morning_report_job(config, db):
    pipeline = AsyncMock()
    morning = AsyncMock()
    morning.generate.return_value = OutreachRequest(
        category=OutreachCategory.DIGEST, topic="Morning Report",
        context="Content", salience_score=0.0, signal_type="morning_report",
    )
    engagement = AsyncMock()
    scheduler = OutreachScheduler(pipeline, morning, engagement, config, db)

    await scheduler._morning_report_job()
    pipeline.submit.assert_called_once()


@pytest.mark.asyncio
async def test_engagement_poll_job(config, db):
    pipeline = AsyncMock()
    morning = AsyncMock()
    engagement = AsyncMock()
    engagement.check_timeouts.return_value = 0
    scheduler = OutreachScheduler(pipeline, morning, engagement, config, db)

    await scheduler._engagement_poll_job()
    engagement.check_timeouts.assert_called_once_with(timeout_hours=24)


# ── Health check job tests ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_health_check_no_alerts_no_send(config, db):
    """When no immediate-escalation alerts fire, pipeline is not called."""
    pipeline = AsyncMock()
    morning = AsyncMock()
    engagement = AsyncMock()
    scheduler = OutreachScheduler(pipeline, morning, engagement, config, db)

    with patch(
        "genesis.outreach.health_outreach.HealthOutreachBridge.check_and_generate",
        new_callable=AsyncMock,
        return_value=[],
    ):
        await scheduler._health_check_job()

    pipeline.submit_raw.assert_not_called()
    pipeline.submit.assert_not_called()


@pytest.mark.asyncio
async def test_health_check_batches_into_one_message(config, db):
    """Multiple immediate alerts should be batched into one submit_raw call."""
    pipeline = AsyncMock()
    pipeline.submit_raw.return_value = OutreachResult(
        outreach_id="test-id",
        status=OutreachStatus.DELIVERED,
        channel="telegram",
        message_content="batched",
        delivery_id="tg-123",
    )
    morning = AsyncMock()
    engagement = AsyncMock()
    scheduler = OutreachScheduler(pipeline, morning, engagement, config, db)

    mock_requests = [
        OutreachRequest(
            category=OutreachCategory.BLOCKER,
            topic="Infrastructure Alert: infra:tmpfs_low",
            context="/tmp at 5% free",
            salience_score=1.0,
            signal_type="health_alert",
            source_id="infra:tmpfs_low",
        ),
        OutreachRequest(
            category=OutreachCategory.BLOCKER,
            topic="Infrastructure Alert: infra:container_memory_high",
            context="Container memory at 93%",
            salience_score=1.0,
            signal_type="health_alert",
            source_id="infra:container_memory_high",
        ),
    ]

    with patch(
        "genesis.outreach.health_outreach.HealthOutreachBridge.check_and_generate",
        new_callable=AsyncMock,
        return_value=mock_requests,
    ):
        await scheduler._health_check_job()

    # Should be ONE submit_raw call, not two submit calls
    pipeline.submit_raw.assert_called_once()
    pipeline.submit.assert_not_called()

    # The batched text should contain both alert messages
    batched_text = pipeline.submit_raw.call_args[0][0]
    assert "/tmp at 5% free" in batched_text
    assert "Container memory at 93%" in batched_text
    assert "INFRASTRUCTURE ALERT" in batched_text
    assert "2 critical alert(s)" in batched_text


@pytest.mark.asyncio
async def test_health_check_single_alert_still_batches(config, db):
    """Even a single alert goes through submit_raw (not submit)."""
    pipeline = AsyncMock()
    pipeline.submit_raw.return_value = OutreachResult(
        outreach_id="test-id",
        status=OutreachStatus.DELIVERED,
        channel="telegram",
        message_content="single",
        delivery_id="tg-456",
    )
    morning = AsyncMock()
    engagement = AsyncMock()
    scheduler = OutreachScheduler(pipeline, morning, engagement, config, db)

    mock_requests = [
        OutreachRequest(
            category=OutreachCategory.BLOCKER,
            topic="Infrastructure Alert: infra:tmpfs_low",
            context="/tmp at 3% free",
            salience_score=1.0,
            signal_type="health_alert",
            source_id="infra:tmpfs_low",
        ),
    ]

    with patch(
        "genesis.outreach.health_outreach.HealthOutreachBridge.check_and_generate",
        new_callable=AsyncMock,
        return_value=mock_requests,
    ):
        await scheduler._health_check_job()

    pipeline.submit_raw.assert_called_once()
    batched_text = pipeline.submit_raw.call_args[0][0]
    assert "/tmp at 3% free" in batched_text
    assert "1 critical alert(s)" in batched_text
