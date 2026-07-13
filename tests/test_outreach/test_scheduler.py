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
        max_daily=5,
        surplus_daily=1,
        content_daily=3,
        notification_daily=10,
        morning_report_time="07:00",
        engagement_timeout_hours=24,
        engagement_poll_minutes=60,
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
        category=OutreachCategory.DIGEST,
        topic="Morning Report",
        context="Content",
        salience_score=0.0,
        signal_type="morning_report",
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


# ── _drain_pending_job: terminal handling + thread routing + age cap ────────


def _drain_pipeline(status):
    pipeline = AsyncMock()
    result = OutreachResult(
        outreach_id="o",
        status=status,
        channel="email",
        message_content="",
    )
    pipeline.submit = AsyncMock(return_value=result)
    pipeline.submit_urgent = AsyncMock(return_value=result)
    return pipeline


async def _remaining(db):
    """Rows still eligible for the next drain (delivered=0)."""
    from genesis.db.crud import pending_outreach

    return await pending_outreach.drain(db, now="2999-01-01T00:00:00+00:00")


@pytest.mark.asyncio
async def test_drain_reconstructs_request_with_thread_and_recipient(config, db):
    """A queued email row must rebuild an OutreachRequest carrying its thread_id
    + validated_recipient — so _deliver routes to the real recipient, not self."""
    from genesis.db.crud import pending_outreach

    await pending_outreach.enqueue(
        db,
        message="follow up",
        category="notification",
        channel="email",
        thread_id="t1",
        validated_recipient="real@prospect.com",
    )
    pipeline = _drain_pipeline(OutreachStatus.DELIVERED)
    scheduler = OutreachScheduler(pipeline, AsyncMock(), AsyncMock(), config, db)

    await scheduler._drain_pending_job()

    pipeline.submit.assert_called_once()
    req = pipeline.submit.call_args[0][0]
    assert req.thread_id == "t1"
    assert req.validated_recipient == "real@prospect.com"


@pytest.mark.asyncio
async def test_drain_held_is_terminal_not_retried(config, db):
    """HELD = handed off to the gate's approval queue; the queue row is done.
    Re-submitting every cycle is exactly the spam multiplier we are killing."""
    from genesis.db.crud import pending_outreach

    await pending_outreach.enqueue(
        db,
        message="x",
        category="notification",
        channel="email",
    )
    pipeline = _drain_pipeline(OutreachStatus.HELD)
    scheduler = OutreachScheduler(pipeline, AsyncMock(), AsyncMock(), config, db)

    await scheduler._drain_pending_job()

    assert await _remaining(db) == []  # marked delivered, not retried


@pytest.mark.asyncio
async def test_drain_ignored_is_terminal(config, db):
    from genesis.db.crud import pending_outreach

    await pending_outreach.enqueue(
        db,
        message="x",
        category="notification",
        channel="email",
    )
    pipeline = _drain_pipeline(OutreachStatus.IGNORED)
    scheduler = OutreachScheduler(pipeline, AsyncMock(), AsyncMock(), config, db)

    await scheduler._drain_pending_job()

    assert await _remaining(db) == []


@pytest.mark.asyncio
async def test_drain_rejected_is_retried(config, db):
    """A transient governance rejection (e.g. quiet_hours) stays queued."""
    from genesis.db.crud import pending_outreach

    await pending_outreach.enqueue(
        db,
        message="x",
        category="notification",
        channel="telegram",
    )
    pipeline = _drain_pipeline(OutreachStatus.REJECTED)
    scheduler = OutreachScheduler(pipeline, AsyncMock(), AsyncMock(), config, db)

    await scheduler._drain_pending_job()

    assert len(await _remaining(db)) == 1  # still pending for next cycle


@pytest.mark.asyncio
async def test_drain_ages_out_perpetually_stuck_row(config, db):
    """A row that never reaches a terminal status must be dropped after 24h
    instead of looping forever (the churn that locked the DB)."""
    from datetime import UTC, datetime, timedelta

    old = (datetime.now(UTC) - timedelta(hours=25)).isoformat()
    await db.execute(
        "INSERT INTO pending_outreach (id, message, category, channel, urgency, "
        "created_at, delivered) VALUES ('old1', 'x', 'notification', 'telegram', "
        "'low', ?, 0)",
        (old,),
    )
    await db.commit()
    pipeline = _drain_pipeline(OutreachStatus.REJECTED)
    scheduler = OutreachScheduler(pipeline, AsyncMock(), AsyncMock(), config, db)

    await scheduler._drain_pending_job()

    pipeline.submit.assert_not_called()  # aged out BEFORE re-submitting
    assert await _remaining(db) == []  # dropped


@pytest.mark.asyncio
async def test_drain_null_id_row_is_cleared_not_looped(config, db):
    """A legacy NULL-id row must be cleared via the rowid fallback, not
    re-drained forever. Before the fix, mark_delivered(WHERE id=NULL) matched
    nothing, so the row (aged out every cycle) never left the queue."""
    from datetime import UTC, datetime, timedelta

    old = (datetime.now(UTC) - timedelta(hours=25)).isoformat()
    # NULL id: omit the id column (SQLite allows NULL in a TEXT PRIMARY KEY).
    await db.execute(
        "INSERT INTO pending_outreach (message, category, channel, urgency, "
        "created_at, delivered) VALUES ('x', 'notification', 'telegram', "
        "'low', ?, 0)",
        (old,),
    )
    await db.commit()
    pipeline = _drain_pipeline(OutreachStatus.REJECTED)
    scheduler = OutreachScheduler(pipeline, AsyncMock(), AsyncMock(), config, db)

    await scheduler._drain_pending_job()

    assert await _remaining(db) == []  # cleared by rowid, not looping forever


@pytest.mark.asyncio
async def test_drain_null_id_row_delivered_terminal(config, db):
    """A deliverable NULL-id row reaches a terminal status and clears by rowid."""
    from datetime import UTC, datetime

    now = datetime.now(UTC).isoformat()
    await db.execute(
        "INSERT INTO pending_outreach (message, category, channel, urgency, "
        "created_at, delivered) VALUES ('x', 'notification', 'telegram', "
        "'low', ?, 0)",
        (now,),
    )
    await db.commit()
    pipeline = _drain_pipeline(OutreachStatus.DELIVERED)
    scheduler = OutreachScheduler(pipeline, AsyncMock(), AsyncMock(), config, db)

    await scheduler._drain_pending_job()

    assert await _remaining(db) == []  # delivered + marked via rowid fallback


@pytest.mark.asyncio
async def test_drain_ages_out_naive_timestamp_row(config, db):
    """A created_at WITHOUT a tz offset must still age out — a naive timestamp
    must not silently bypass the cap (and loop forever)."""
    from datetime import UTC, datetime, timedelta

    old_naive = (datetime.now(UTC) - timedelta(hours=25)).replace(tzinfo=None).isoformat()
    assert "+00:00" not in old_naive  # genuinely naive
    await db.execute(
        "INSERT INTO pending_outreach (id, message, category, channel, urgency, "
        "created_at, delivered) VALUES ('oldnaive', 'x', 'notification', "
        "'telegram', 'low', ?, 0)",
        (old_naive,),
    )
    await db.commit()
    pipeline = _drain_pipeline(OutreachStatus.REJECTED)
    scheduler = OutreachScheduler(pipeline, AsyncMock(), AsyncMock(), config, db)

    await scheduler._drain_pending_job()

    pipeline.submit.assert_not_called()
    assert await _remaining(db) == []


# ── ambient health alert gating (cause-aware state machine) ─────────────────
# governance dedups ambient_health with window=0 ("the monitor's state machine
# gates re-alerts") — so THIS state machine is the only thing standing between
# the user and a silently-swallowed second fault.


def _ambient_snapshot(**overrides):
    from datetime import UTC, datetime

    base = {
        "ts": datetime.now(UTC).isoformat(),
        "active_connections": 1,
        "diar_enabled": True,
        "diar_worker_alive": True,
    }
    base.update(overrides)
    return base


async def _ambient_tick(scheduler, snapshot):
    with (
        patch(
            "genesis.observability.ambient_health.load_ambient_remote_config",
            return_value=object(),
        ),
        patch(
            "genesis.observability.ambient_health.read_edge_health",
            AsyncMock(return_value=snapshot),
        ),
    ):
        await scheduler._ambient_health_job()


def _make_scheduler(config, db):
    return OutreachScheduler(AsyncMock(), AsyncMock(), AsyncMock(), config, db)


@pytest.mark.asyncio
async def test_ambient_new_cause_realerts_while_already_degraded(config, db):
    scheduler = _make_scheduler(config, db)

    # Tick 1: diar worker dead -> degraded, alert fires.
    await _ambient_tick(scheduler, _ambient_snapshot(diar_worker_alive=False))
    assert scheduler._pipeline.submit_raw.call_count == 1

    # Tick 2: diar recovered, but an INDEPENDENT fault (RSS regression)
    # appeared while still degraded — a bare status-edge gate would swallow
    # it and the user would never hear about the leak.
    await _ambient_tick(scheduler, _ambient_snapshot(rss_total_mb=1200.0))
    assert scheduler._pipeline.submit_raw.call_count == 2
    assert "RSS" in scheduler._pipeline.submit_raw.call_args[0][0]


@pytest.mark.asyncio
async def test_ambient_same_cause_does_not_nag(config, db):
    scheduler = _make_scheduler(config, db)
    await _ambient_tick(scheduler, _ambient_snapshot(rss_total_mb=1200.0))
    # Same cause next tick, different live value — must NOT re-alert.
    await _ambient_tick(scheduler, _ambient_snapshot(rss_total_mb=1300.0))
    assert scheduler._pipeline.submit_raw.call_count == 1


@pytest.mark.asyncio
async def test_ambient_down_then_new_degraded_cause_alerts(config, db):
    from datetime import UTC, datetime, timedelta

    scheduler = _make_scheduler(config, db)
    stale = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
    await _ambient_tick(scheduler, _ambient_snapshot(ts=stale))  # down, alert 1
    # Heartbeat recovers but lands straight on an RSS breach: new cause, alert 2.
    await _ambient_tick(scheduler, _ambient_snapshot(rss_total_mb=1200.0))
    assert scheduler._pipeline.submit_raw.call_count == 2


@pytest.mark.asyncio
async def test_ambient_recovery_then_rebreach_realerts(config, db):
    scheduler = _make_scheduler(config, db)
    await _ambient_tick(scheduler, _ambient_snapshot(rss_total_mb=1200.0))  # alert 1
    await _ambient_tick(scheduler, _ambient_snapshot())  # recovery notice (2)
    await _ambient_tick(scheduler, _ambient_snapshot(rss_total_mb=1200.0))  # alert 3
    assert scheduler._pipeline.submit_raw.call_count == 3


@pytest.mark.asyncio
async def test_ambient_rss_alert_text_names_leak_not_down(config, db):
    scheduler = _make_scheduler(config, db)
    await _ambient_tick(scheduler, _ambient_snapshot(rss_total_mb=1200.0))
    text = scheduler._pipeline.submit_raw.call_args[0][0]
    assert "down/hung" not in text  # the bridge is alive — don't misdiagnose
    assert "RSS" in text
