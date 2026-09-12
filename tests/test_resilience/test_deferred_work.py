"""Tests for DeferredWorkQueue."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from genesis.resilience.deferred_work import (
    DISCARD,
    DRAIN,
    FOREGROUND,
    MEMORY_OPS,
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

    @pytest.mark.asyncio
    async def test_next_pending_filters_by_work_type(self, queue):
        # WS-6 head-of-line: a higher-priority outreach item (20) sits ahead of
        # a reflection (30). Without a filter the consumer is blocked by it.
        await queue.enqueue("outreach_delivery", None, 20, "{}", "reason")
        await queue.enqueue("reflection", None, REFLECTION, "{}", "reason")

        top = await queue.next_pending(max_priority=40)
        assert top["work_type"] == "outreach_delivery"  # head-of-line blocker

        refl = await queue.next_pending(work_type="reflection", max_priority=40)
        assert refl is not None
        assert refl["work_type"] == "reflection"  # reached past the blocker


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


class TestHasOpen:
    """Dedup-at-defer guard — count_open_by_identity / has_open. Identity is the
    full (topic, category, signal_type) governance uses, NOT topic alone."""

    @staticmethod
    def _payload(topic="alert:x", category="alert", signal_type="sig"):
        import json as _json

        return _json.dumps(
            {"topic": topic, "category": category, "signal_type": signal_type}
        )

    async def _enqueue(self, queue, **ident):
        return await queue.enqueue(
            "outreach_delivery", None, REFLECTION, self._payload(**ident), "r"
        )

    @pytest.mark.asyncio
    async def test_pending_row_is_open(self, queue):
        await self._enqueue(queue)
        assert await queue.has_open("outreach_delivery", "alert:x", "alert", "sig")

    @pytest.mark.asyncio
    async def test_processing_row_is_open(self, queue):
        # The recovery worker marks the original 'processing' before re-attempting;
        # a re-defer during that window must still see it as open.
        item_id = await self._enqueue(queue)
        await queue.mark_processing(item_id)
        assert await queue.has_open("outreach_delivery", "alert:x", "alert", "sig")

    @pytest.mark.asyncio
    async def test_terminal_rows_are_not_open(self, queue):
        completed_id = await self._enqueue(queue, topic="alert:c")
        await queue.mark_completed(completed_id)
        assert not await queue.has_open("outreach_delivery", "alert:c", "alert", "sig")

        discarded_id = await self._enqueue(queue, topic="alert:d")
        await queue.mark_discarded(discarded_id, "gone")
        assert not await queue.has_open("outreach_delivery", "alert:d", "alert", "sig")

    @pytest.mark.asyncio
    async def test_distinguishes_topics(self, queue):
        await self._enqueue(queue)
        assert not await queue.has_open("outreach_delivery", "alert:y", "alert", "sig")

    @pytest.mark.asyncio
    async def test_distinguishes_work_types(self, queue):
        await self._enqueue(queue)
        assert not await queue.has_open("reflection", "alert:x", "alert", "sig")

    @pytest.mark.asyncio
    async def test_same_topic_different_category_not_open(self, queue):
        # Codex P1: a topic is reused across message types (executor emits
        # progress/success/alert/blocker under one 'Task <id>' topic). Keying on
        # category (and signal_type) keeps a later blocker independently
        # deliverable instead of being dropped behind an open progress row.
        await self._enqueue(queue, category="notification")
        assert not await queue.has_open(
            "outreach_delivery", "alert:x", "blocker", "sig"
        )
        # ...but the exact same identity IS a dup.
        assert await queue.has_open(
            "outreach_delivery", "alert:x", "notification", "sig"
        )

    @pytest.mark.asyncio
    async def test_same_topic_different_signal_type_not_open(self, queue):
        await self._enqueue(queue, signal_type="progress")
        assert not await queue.has_open(
            "outreach_delivery", "alert:x", "alert", "blocker_signal"
        )

    @pytest.mark.asyncio
    async def test_malformed_payload_row_does_not_raise(self, queue):
        # Codex P2: a corrupt payload_json must be skipped, not raise
        # OperationalError (which _defer's broad handler would swallow, silently
        # dropping EVERY new delivery until the bad row is cleared).
        await queue.enqueue(
            "outreach_delivery", None, REFLECTION, "not valid json{", "r"
        )
        await self._enqueue(queue)  # a valid dup alongside the corrupt row
        assert await queue.has_open("outreach_delivery", "alert:x", "alert", "sig")
        assert not await queue.has_open("outreach_delivery", "nope", "alert", "sig")

    @pytest.mark.asyncio
    async def test_null_signal_type_matches_cleanly(self, queue):
        import json as _json

        await queue.enqueue(
            "outreach_delivery",
            None,
            REFLECTION,
            _json.dumps({"topic": "alert:x", "category": "alert"}),  # no signal_type
            "r",
        )
        assert await queue.has_open("outreach_delivery", "alert:x", "alert", None)

    @pytest.mark.asyncio
    async def test_counts_multiple_open(self, queue):
        from genesis.db.crud import deferred_work as crud

        await self._enqueue(queue)
        await self._enqueue(queue)
        assert (
            await crud.count_open_by_identity(
                queue._db,
                work_type="outreach_delivery",
                topic="alert:x",
                category="alert",
                signal_type="sig",
            )
            == 2
        )


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


class TestSupersede:
    @pytest.mark.asyncio
    async def test_deletes_batch_but_preserves_processing(self, queue):
        """supersede removes a work_type's pending/completed residue but never
        yanks an in-flight (processing) item out from under its worker."""
        a = await queue.enqueue("dream_synthesis_slice", None, MEMORY_OPS, "{}", "weekly")
        b = await queue.enqueue("dream_synthesis_slice", None, MEMORY_OPS, "{}", "weekly")
        c = await queue.enqueue("dream_synthesis_slice", None, MEMORY_OPS, "{}", "weekly")
        await queue.enqueue("reflection", None, REFLECTION, "{}", "cloud_down")
        await queue.mark_completed(a)
        await queue.mark_processing(b)

        removed = await queue.supersede("dream_synthesis_slice")

        # completed a + pending c removed; processing b preserved
        assert removed == 2
        assert await queue.count_pending("dream_synthesis_slice") == 0
        cursor = await queue._db.execute(
            "SELECT id, status FROM deferred_work_queue WHERE work_type = ?",
            ("dream_synthesis_slice",),
        )
        rows = await cursor.fetchall()
        assert [(r["id"], r["status"]) for r in rows] == [(b, "processing")]
        assert c not in [r["id"] for r in rows]
        # other work_types untouched
        assert await queue.count_pending("reflection") == 1

    @pytest.mark.asyncio
    async def test_supersede_empty_returns_zero(self, queue):
        assert await queue.supersede("dream_synthesis_slice") == 0
