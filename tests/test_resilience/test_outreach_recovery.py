"""Tests for OutreachRecoveryWorker."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from genesis.resilience.outreach_recovery import (
    _BACKOFF_SCHEDULE,
    _MAX_RETRIES,
    OutreachRecoveryWorker,
)


def _make_item(
    *,
    item_id: str = "item-1",
    attempts: int = 0,
    last_attempt_at: str | None = None,
    category: str = "alert",
    channel: str = "telegram",
    content: str = "Test alert",
    topic: str = "test",
) -> dict:
    return {
        "id": item_id,
        "work_type": "outreach_delivery",
        "status": "pending",
        "attempts": attempts,
        "last_attempt_at": last_attempt_at,
        "payload_json": json.dumps({
            "outreach_id": "out-1",
            "channel": channel,
            "content": content,
            "category": category,
            "topic": topic,
        }),
        "deferred_reason": "adapter down",
    }


@pytest.fixture
def worker():
    queue = AsyncMock()
    pipeline = AsyncMock()
    db = AsyncMock()
    return OutreachRecoveryWorker(queue=queue, pipeline=pipeline, db=db)


@pytest.mark.asyncio
async def test_successful_retry(worker):
    """First attempt succeeds — item marked completed."""
    from genesis.outreach.types import OutreachStatus

    result = MagicMock()
    result.status = OutreachStatus.DELIVERED
    result.error = None
    worker._pipeline.submit_raw = AsyncMock(return_value=result)

    item = _make_item()
    await worker._retry(item)

    worker._queue.mark_processing.assert_awaited_once_with("item-1")
    worker._queue.mark_completed.assert_awaited_once_with("item-1")


@pytest.mark.asyncio
async def test_failed_retry_resets_to_pending(worker):
    """Failed attempt resets to pending for next backoff cycle."""
    from genesis.outreach.types import OutreachStatus

    result = MagicMock()
    result.status = OutreachStatus.FAILED
    result.error = "adapter down"
    worker._pipeline.submit_raw = AsyncMock(return_value=result)

    item = _make_item(attempts=2)
    await worker._retry(item)

    worker._queue.mark_processing.assert_awaited_once()
    worker._queue.reset_to_pending.assert_awaited_once_with("item-1")


@pytest.mark.asyncio
async def test_exhausted_creates_observation(worker):
    """After max retries, item is discarded and observation created."""
    item = _make_item(attempts=_MAX_RETRIES)

    # Mock the observation create
    import genesis.db.crud.observations as obs_crud
    obs_crud.create = AsyncMock()

    await worker._exhaust(item)

    worker._queue.mark_discarded.assert_awaited_once()
    assert "exhausted" in worker._queue.mark_discarded.call_args[0][1].lower()


@pytest.mark.asyncio
async def test_backoff_skips_too_soon(worker):
    """Items within backoff window are skipped."""
    from unittest.mock import patch

    # Last attempt was 30s ago, backoff for attempt 1 is 60s
    recent = (datetime.now(UTC) - timedelta(seconds=30)).isoformat()
    item = _make_item(attempts=1, last_attempt_at=recent)

    with patch("genesis.db.crud.deferred_work.query_pending", new_callable=AsyncMock) as mock_query:
        mock_query.return_value = [item]
        await worker._process_pending()

    # Should NOT have called mark_processing (skipped due to backoff)
    worker._queue.mark_processing.assert_not_awaited()


@pytest.mark.asyncio
async def test_backoff_allows_after_window(worker):
    """Items past backoff window are retried."""
    from unittest.mock import patch

    from genesis.outreach.types import OutreachStatus

    # Last attempt was 120s ago, backoff for attempt 1 is 60s → should retry
    old_enough = (datetime.now(UTC) - timedelta(seconds=120)).isoformat()
    item = _make_item(attempts=1, last_attempt_at=old_enough)

    result = MagicMock()
    result.status = OutreachStatus.DELIVERED
    result.error = None
    worker._pipeline.submit_raw = AsyncMock(return_value=result)

    with patch("genesis.db.crud.deferred_work.query_pending", new_callable=AsyncMock) as mock_query:
        mock_query.return_value = [item]
        await worker._process_pending()

    worker._queue.mark_processing.assert_awaited_once()


@pytest.mark.asyncio
async def test_unparseable_payload_discarded(worker):
    """Items with garbage payloads are discarded immediately."""
    item = _make_item()
    item["payload_json"] = "not json{"

    await worker._retry(item)

    worker._queue.mark_discarded.assert_awaited_once()
    assert "unparseable" in worker._queue.mark_discarded.call_args[0][1].lower()


def test_backoff_schedule_matches_spec():
    """Backoff schedule matches the design: 1m, 5m, 15m, 1h, 1h."""
    assert _BACKOFF_SCHEDULE == (60, 300, 900, 3600, 3600)
    assert _MAX_RETRIES == 5
