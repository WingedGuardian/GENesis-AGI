"""Tests for origin-targeted Telegram delivery in the outreach chokepoint.

The delivery model routes a background session's result back to the exact
conversation it was requested in by pinning target_chat_id + target_thread_id on
the OutreachRequest. _deliver must honor that BEFORE the category->forum-topic
routing, and must not regress the existing (untargeted) category path.
"""

from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest

from genesis.content.types import FormatTarget, FormattedContent
from genesis.db.schema import create_all_tables
from genesis.outreach.config import OutreachConfig, QuietHours
from genesis.outreach.governance import GovernanceGate
from genesis.outreach.pipeline import OutreachPipeline
from genesis.outreach.types import OutreachCategory, OutreachRequest, OutreachStatus


@pytest.fixture
def config():
    return OutreachConfig(
        quiet_hours=QuietHours(start="22:00", end="07:00"),
        channel_preferences={"default": "telegram"},
        thresholds={"blocker": 0.0, "alert": 0.3, "surplus": 0.7, "digest": 0.0},
        max_daily=50,
        surplus_daily=10,
        content_daily=30,
        notification_daily=100,
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


@pytest.fixture
def mock_formatter():
    formatter = MagicMock()
    formatter.format.side_effect = lambda text, target: FormattedContent(
        text=text,
        target=FormatTarget.TELEGRAM,
        truncated=False,
        original_length=len(text),
    )
    return formatter


@pytest.fixture
def mock_channel():
    adapter = AsyncMock()
    adapter.send_message.return_value = "delivery-abc"
    return adapter


def _pipeline(config, db, mock_formatter, mock_channel, *, recipients):
    return OutreachPipeline(
        governance=GovernanceGate(config, db),
        drafter=AsyncMock(),
        formatter=mock_formatter,
        channels={"telegram": mock_channel},
        db=db,
        config=config,
        recipients=recipients,
    )


def _targeted_req(*, chat_id, thread_id):
    return OutreachRequest(
        category=OutreachCategory.ALERT,
        topic="bg_result:s1",
        context="the delivered result",
        salience_score=0.9,
        channel="telegram",
        verbatim=True,
        target_chat_id=chat_id,
        target_thread_id=thread_id,
    )


@pytest.mark.asyncio
async def test_targeted_dm_delivery(config, db, mock_formatter, mock_channel):
    pipeline = _pipeline(config, db, mock_formatter, mock_channel, recipients={"telegram": "999"})
    result = await pipeline.submit_urgent(_targeted_req(chat_id="12345678", thread_id=None))

    assert result.status == OutreachStatus.DELIVERED
    args, kwargs = mock_channel.send_message.call_args
    assert args[0] == "12345678"  # delivered to the target chat, NOT the default "999"
    assert kwargs["message_thread_id"] is None


@pytest.mark.asyncio
async def test_targeted_forum_topic_delivery(config, db, mock_formatter, mock_channel):
    pipeline = _pipeline(config, db, mock_formatter, mock_channel, recipients={"telegram": "999"})
    pipeline.set_forum_chat_id(-1002000)
    result = await pipeline.submit_urgent(_targeted_req(chat_id="-1002000", thread_id=110))

    assert result.status == OutreachStatus.DELIVERED
    args, kwargs = mock_channel.send_message.call_args
    assert args[0] == "-1002000"
    assert kwargs["message_thread_id"] == 110


@pytest.mark.asyncio
async def test_targeted_survives_empty_default_recipient(config, db, mock_formatter, mock_channel):
    # No default telegram recipient configured — a targeted send must still
    # deliver (the target chat satisfies the non-empty recipient guard).
    pipeline = _pipeline(config, db, mock_formatter, mock_channel, recipients={})
    result = await pipeline.submit_urgent(_targeted_req(chat_id="555", thread_id=None))

    assert result.status == OutreachStatus.DELIVERED
    args, _ = mock_channel.send_message.call_args
    assert args[0] == "555"


@pytest.mark.asyncio
async def test_untargeted_category_path_unchanged(config, db, mock_formatter, mock_channel):
    # No target → existing behavior: DM fallback to the default recipient
    # (topic_manager is None, so no forum topic resolves).
    pipeline = _pipeline(config, db, mock_formatter, mock_channel, recipients={"telegram": "12345"})
    req = OutreachRequest(
        category=OutreachCategory.ALERT,
        topic="ordinary alert",
        context="hello",
        salience_score=0.9,
        channel="telegram",
        verbatim=True,
    )
    result = await pipeline.submit_urgent(req)

    assert result.status == OutreachStatus.DELIVERED
    args, kwargs = mock_channel.send_message.call_args
    assert args[0] == "12345"
    assert kwargs["message_thread_id"] is None


@pytest.mark.asyncio
async def test_defer_carries_target_fields_for_retry(config, db, mock_formatter):
    # A transient send failure defers the send; the origin-target fields MUST be
    # serialized so a retry still reaches the exact origin thread (SHOULD-FIX 1).
    import json

    failing_adapter = AsyncMock()
    failing_adapter.send_message.side_effect = RuntimeError("transient telegram error")
    deferred_queue = AsyncMock()
    pipeline = OutreachPipeline(
        governance=GovernanceGate(config, db),
        drafter=AsyncMock(),
        formatter=mock_formatter,
        channels={"telegram": failing_adapter},
        db=db,
        config=config,
        recipients={"telegram": "999"},
        deferred_queue=deferred_queue,
    )
    await pipeline.submit_urgent(_targeted_req(chat_id="12345678", thread_id=None))

    deferred_queue.enqueue.assert_awaited_once()
    payload = json.loads(deferred_queue.enqueue.call_args.kwargs["payload"])
    assert payload["target_chat_id"] == "12345678"
    assert payload["target_thread_id"] is None


@pytest.mark.asyncio
async def test_submit_raw_retry_path_honors_target(config, db, mock_formatter, mock_channel):
    # The recovery worker retries via submit_raw(content, request); a request
    # carrying the restored target fields must still deliver to the origin thread.
    pipeline = _pipeline(config, db, mock_formatter, mock_channel, recipients={"telegram": "999"})
    pipeline.set_forum_chat_id(-1002000)
    req = _targeted_req(chat_id="-1002000", thread_id=110)
    result = await pipeline.submit_raw("pre-formatted result", req)

    assert result.status == OutreachStatus.DELIVERED
    args, kwargs = mock_channel.send_message.call_args
    assert args[0] == "-1002000"
    assert kwargs["message_thread_id"] == 110
