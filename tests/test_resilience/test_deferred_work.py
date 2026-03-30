"""Tests for DeferredWorkQueue."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from genesis.resilience.deferred_work import (
    DISCARD,
    DRAIN,
    FOREGROUND,
    MORNING_REPORT,
    REFLECTION,
    REFRESH,
    SURPLUS,
    TTL,
    DeferredWorkQueue,
)


@pytest.fixture
async def queue(db):
    clock_time = [datetime(2026, 3, 11, 12, 0, 0, tzinfo=UTC)]
    def clock():
        return clock_time[0]
    q = DeferredWorkQueue(db, clock=clock)
    q._advance = lambda s: clock_time.__setitem__(0, clock_time[0] + timedelta(seconds=s))
    return q


class TestEnqueueDequeue:
    @pytest.mark.asyncio
    async def test_enqueue_returns_id(self, queue):
        item_id = await queue.enqueue(
            "reflection", None, REFLECTION, '{"data": 1}', "cloud_down",
        )
        assert isinstance(item_id, str)
        assert len(item_id) == 36  # UUID

    @pytest.mark.asyncio
    async def test_next_pending_returns_highest_priority(self, queue):
        await queue.enqueue("surplus", None, SURPLUS, '{}', "reason")
        await queue.enqueue("reflection", None, REFLECTION, '{}', "reason")
        await queue.enqueue("foreground", None, FOREGROUND, '{}', "reason")

        item = await queue.next_pending()
        assert item is not None
        assert item["work_type"] == "foreground"
        assert item["priority"] == FOREGROUND

    @pytest.mark.asyncio
    async def test_next_pending_empty(self, queue):
        item = await queue.next_pending()
        assert item is None

    @pytest.mark.asyncio
    async def test_max_priority_filter(self, queue):
        await queue.enqueue("surplus", None, SURPLUS, '{}', "reason")
        await queue.enqueue("foreground", None, FOREGROUND, '{}', "reason")

        item = await queue.next_pending(max_priority=FOREGROUND)
        assert item["work_type"] == "foreground"

        # Only foreground should match, surplus has higher priority number
        item2 = await queue.next_pending(max_priority=5)
        assert item2 is None


class TestMarkStatus:
    @pytest.mark.asyncio
    async def test_mark_processing(self, queue):
        item_id = await queue.enqueue("test", None, REFLECTION, '{}', "reason")
        assert await queue.mark_processing(item_id)
        # Should no longer appear as pending
        assert await queue.count_pending() == 0

    @pytest.mark.asyncio
    async def test_mark_completed(self, queue):
        item_id = await queue.enqueue("test", None, REFLECTION, '{}', "reason")
        assert await queue.mark_completed(item_id)
        assert await queue.count_pending() == 0

    @pytest.mark.asyncio
    async def test_mark_discarded(self, queue):
        item_id = await queue.enqueue("test", None, REFLECTION, '{}', "reason")
        assert await queue.mark_discarded(item_id, "no longer needed")
        assert await queue.count_pending() == 0


class TestStalenessExpiry:
    @pytest.mark.asyncio
    async def test_drain_never_expires(self, queue):
        await queue.enqueue("test", None, REFLECTION, '{}', "reason", staleness_policy=DRAIN)
        expired = await queue.expire_stale()
        assert expired == 0
        assert await queue.count_pending() == 1

    @pytest.mark.asyncio
    async def test_refresh_always_expires(self, queue):
        await queue.enqueue("test", None, MORNING_REPORT, '{}', "reason", staleness_policy=REFRESH)
        expired = await queue.expire_stale()
        assert expired == 1
        assert await queue.count_pending() == 0

    @pytest.mark.asyncio
    async def test_discard_always_expires(self, queue):
        await queue.enqueue("test", None, SURPLUS, '{}', "reason", staleness_policy=DISCARD)
        expired = await queue.expire_stale()
        assert expired == 1

    @pytest.mark.asyncio
    async def test_ttl_expires_when_old(self, queue):
        await queue.enqueue(
            "test", None, REFLECTION, '{}', "reason",
            staleness_policy=TTL, staleness_ttl_s=300,
        )
        # Advance clock past TTL
        queue._advance(600)
        expired = await queue.expire_stale()
        assert expired == 1

    @pytest.mark.asyncio
    async def test_ttl_not_expired_when_fresh(self, queue):
        await queue.enqueue(
            "test", None, REFLECTION, '{}', "reason",
            staleness_policy=TTL, staleness_ttl_s=300,
        )
        # Don't advance clock
        expired = await queue.expire_stale()
        assert expired == 0
        assert await queue.count_pending() == 1


class TestDrainByPriority:
    @pytest.mark.asyncio
    async def test_drain_ordering(self, queue):
        await queue.enqueue("surplus", None, SURPLUS, '{"n":1}', "reason")
        await queue.enqueue("foreground", None, FOREGROUND, '{"n":2}', "reason")
        await queue.enqueue("reflection", None, REFLECTION, '{"n":3}', "reason")

        items = await queue.drain_by_priority(limit=10)
        assert len(items) == 3
        assert items[0]["work_type"] == "foreground"
        assert items[1]["work_type"] == "reflection"
        assert items[2]["work_type"] == "surplus"

    @pytest.mark.asyncio
    async def test_drain_limit(self, queue):
        for i in range(5):
            await queue.enqueue(f"type_{i}", None, SURPLUS, '{}', "reason")
        items = await queue.drain_by_priority(limit=2)
        assert len(items) == 2


class TestCountPending:
    @pytest.mark.asyncio
    async def test_count_all(self, queue):
        await queue.enqueue("a", None, FOREGROUND, '{}', "reason")
        await queue.enqueue("b", None, SURPLUS, '{}', "reason")
        assert await queue.count_pending() == 2

    @pytest.mark.asyncio
    async def test_count_by_type(self, queue):
        await queue.enqueue("a", None, FOREGROUND, '{}', "reason")
        await queue.enqueue("a", None, SURPLUS, '{}', "reason")
        await queue.enqueue("b", None, SURPLUS, '{}', "reason")
        assert await queue.count_pending(work_type="a") == 2
        assert await queue.count_pending(work_type="b") == 1
