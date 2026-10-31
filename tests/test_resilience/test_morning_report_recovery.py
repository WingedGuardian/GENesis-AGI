"""Recovered reports retain history identity without resending legacy retries."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from genesis.content.formatter import ContentFormatter
from genesis.db.crud import deferred_work, outreach
from genesis.outreach.config import QUIET_HOURS_DISABLED, OutreachConfig
from genesis.outreach.governance import GovernanceGate
from genesis.outreach.morning_report import MorningReportGenerator
from genesis.outreach.pipeline import OutreachPipeline
from genesis.outreach.types import OutreachCategory, OutreachRequest, OutreachStatus
from genesis.resilience.deferred_work import DeferredWorkQueue
from genesis.resilience.outreach_recovery import OutreachRecoveryWorker

TOPIC = "Morning Report — 2026-10-29"
CONTENT = "## Urgent\n- Synthetic recovery item remains open.\n"


@pytest.fixture
def delivery(db):
    config = OutreachConfig(
        quiet_hours=QUIET_HOURS_DISABLED, channel_preferences={"default": "telegram"},
        thresholds={}, max_daily=50, surplus_daily=50, content_daily=50,
        notification_daily=50, morning_report_time="07:00",
        engagement_timeout_hours=24, engagement_poll_minutes=60,
    )
    adapter = AsyncMock()
    adapter.send_message.return_value = "synthetic-delivery"
    queue = DeferredWorkQueue(db)
    gate = GovernanceGate(config, db)
    pipeline = OutreachPipeline(
        governance=gate, drafter=AsyncMock(), formatter=ContentFormatter(),
        channels={"telegram": adapter}, db=db, config=config,
        recipients={"telegram": "synthetic-recipient"}, deferred_queue=queue,
    )
    worker = OutreachRecoveryWorker(queue=queue, pipeline=pipeline, db=db)
    return pipeline, worker, adapter, gate


def report_request(*, signal="morning_report", topic=TOPIC):
    return OutreachRequest(
        category=OutreachCategory.DIGEST, topic=topic, context="",
        salience_score=0.9, signal_type=signal, channel="telegram",
    )


async def failed_report(db, pipeline, adapter, *, signal="morning_report"):
    adapter.send_message.side_effect = OSError("synthetic outage")
    result = await pipeline.submit_raw(CONTENT, report_request(signal=signal))
    assert result.status == OutreachStatus.FAILED
    items = await deferred_work.query_pending(db, work_type="outreach_delivery")
    assert len(items) == 1
    adapter.send_message.side_effect = None
    return items[0]


async def test_recovered_report_reaches_carryover_and_keeps_date_dedup(db, delivery):
    pipeline, worker, adapter, _gate = delivery
    item = await failed_report(db, pipeline, adapter)
    await worker._retry(item)
    previous = await outreach.get_previous_report(db)
    assert previous is not None
    assert previous["message_content"] == CONTENT
    generator = MorningReportGenerator(AsyncMock(), db, AsyncMock())
    assert "Synthetic recovery item" in await generator._get_previous_report_carryover()
    assert not await deferred_work.query_pending(db, work_type="outreach_delivery")
    same = await pipeline.submit_raw(CONTENT, report_request())
    assert same.status == OutreachStatus.REJECTED
    other = await pipeline.submit_raw(CONTENT, report_request(topic="Morning Report — 2026-10-30"))
    assert other.status == OutreachStatus.DELIVERED
    assert adapter.send_message.await_count == 3  # initial failure + two distinct reports


async def test_old_delivered_retry_with_uncompleted_queue_is_not_resent(db, delivery):
    pipeline, worker, adapter, _gate = delivery
    item = await failed_report(db, pipeline, adapter)
    # Old worker delivered under deferred_retry but failed to finish its queue row.
    await worker._queue.mark_processing(item["id"])
    old = await pipeline.submit_raw(CONTENT, report_request(signal="deferred_retry"))
    assert old.status == OutreachStatus.DELIVERED
    await worker._queue.reset_to_pending(item["id"])
    await worker._retry(item)
    assert adapter.send_message.await_count == 2
    assert not await deferred_work.query_pending(db, work_type="outreach_delivery")
    # Compatibility is dedup only: do not migrate old history into carryover.
    assert await outreach.get_previous_report(db) is None


@pytest.mark.parametrize("signal", [None, "alert", "unknown_signal"])
async def test_other_retries_keep_legacy_signal(db, delivery, signal):
    pipeline, worker, adapter, _gate = delivery
    item = await failed_report(db, pipeline, adapter, signal=signal)
    await worker._retry(item)
    rows = await (await db.execute("SELECT signal_type FROM outreach_history")).fetchall()
    assert [row[0] for row in rows] == ["deferred_retry"]


async def test_failed_morning_retry_keeps_single_queue_owner(db, delivery):
    pipeline, worker, adapter, _gate = delivery
    item = await failed_report(db, pipeline, adapter)
    adapter.send_message.side_effect = OSError("still offline")
    await worker._retry(item)
    rows = await deferred_work.query_pending(db, work_type="outreach_delivery")
    assert [row["id"] for row in rows] == [item["id"]]
    assert rows[0]["attempts"] == 1
    assert await outreach.get_previous_report(db) is None


@pytest.mark.parametrize(
    ("old_signal", "topic", "category", "age", "delivered", "incoming", "duplicate"),
    [
        ("deferred_retry", TOPIC, "digest", 1, True, "morning_report", True),
        ("deferred_retry", TOPIC, "digest", 25, True, "morning_report", False),
        ("deferred_retry", TOPIC, "digest", 1, False, "morning_report", False),
        ("deferred_retry", TOPIC + " other", "digest", 1, True, "morning_report", False),
        ("deferred_retry", TOPIC, "alert", 1, True, "morning_report", False),
        ("other_signal", TOPIC, "digest", 1, True, "morning_report", False),
        ("deferred_retry", TOPIC, "digest", 1, True, "other_signal", False),
        ("morning_report", TOPIC, "digest", 1, True, "deferred_retry", False),
    ],
)
async def test_legacy_report_dedup_boundaries(
    db, delivery, old_signal, topic, category, age, delivered, incoming, duplicate,
):
    _pipeline, _worker, _adapter, gate = delivery
    when = (datetime.now(UTC) - timedelta(hours=age)).isoformat()
    await outreach.create(
        db, id="synthetic-history", signal_type=old_signal, topic=topic,
        category=category, salience_score=0.9, channel="telegram",
        message_content=CONTENT, created_at=when,
    )
    if delivered:
        await outreach.record_delivery(db, "synthetic-history", delivered_at=when)
    assert await gate.is_duplicate(report_request(signal=incoming)) is duplicate
