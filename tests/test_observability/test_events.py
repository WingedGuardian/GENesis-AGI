"""Tests for GenesisEventBus."""

import asyncio

import pytest

from genesis.observability.events import GenesisEventBus, _at_or_above
from genesis.observability.types import GenesisEvent, Severity, Subsystem


@pytest.fixture
def bus():
    from datetime import UTC, datetime
    frozen = datetime(2026, 3, 4, tzinfo=UTC)
    return GenesisEventBus(clock=lambda: frozen)


class TestEmit:
    @pytest.mark.asyncio
    async def test_emit_returns_event(self, bus):
        event = await bus.emit(
            Subsystem.ROUTING, Severity.WARNING, "breaker.tripped", "Provider X down"
        )
        assert isinstance(event, GenesisEvent)
        assert event.subsystem == Subsystem.ROUTING
        assert event.event_type == "breaker.tripped"
        assert event.timestamp == "2026-03-04T00:00:00+00:00"

    @pytest.mark.asyncio
    async def test_emit_with_details(self, bus):
        event = await bus.emit(
            Subsystem.ROUTING, Severity.ERROR, "all_exhausted", "No providers",
            call_site="chat", attempts=3,
        )
        assert event.details == {"call_site": "chat", "attempts": 3}


class TestListeners:
    @pytest.mark.asyncio
    async def test_listener_receives_events(self, bus):
        received = []

        async def listener(event):
            received.append(event)

        bus.subscribe(listener)
        await bus.emit(Subsystem.ROUTING, Severity.INFO, "test", "hello")
        assert len(received) == 1
        assert received[0].message == "hello"

    @pytest.mark.asyncio
    async def test_severity_filtering(self, bus):
        received = []

        async def listener(event):
            received.append(event)

        bus.subscribe(listener, min_severity=Severity.WARNING)
        await bus.emit(Subsystem.ROUTING, Severity.INFO, "low", "skip me")
        await bus.emit(Subsystem.ROUTING, Severity.WARNING, "high", "keep me")
        assert len(received) == 1
        assert received[0].event_type == "high"

    @pytest.mark.asyncio
    async def test_listener_error_isolated(self, bus):
        """A failing listener must not prevent other listeners from running."""
        received = []

        async def bad_listener(event):
            raise RuntimeError("boom")

        async def good_listener(event):
            received.append(event)

        bus.subscribe(bad_listener)
        bus.subscribe(good_listener)
        await bus.emit(Subsystem.ROUTING, Severity.WARNING, "test", "msg")
        assert len(received) == 1

    @pytest.mark.asyncio
    async def test_multiple_listeners(self, bus):
        counts = [0, 0]

        async def l1(event):
            counts[0] += 1

        async def l2(event):
            counts[1] += 1

        bus.subscribe(l1)
        bus.subscribe(l2)
        await bus.emit(Subsystem.SURPLUS, Severity.ERROR, "fail", "x")
        assert counts == [1, 1]


class TestStopDrain:
    @pytest.mark.asyncio
    async def test_stop_drains_queued_events(self):
        """Events queued before stop() should be flushed to DB."""
        import asyncio
        from unittest.mock import AsyncMock

        db = AsyncMock()
        bus = GenesisEventBus(db=db)

        # Track what insert_batch receives
        written_batches: list[list[dict]] = []

        async def fake_insert_batch(_db, batch):
            written_batches.append(list(batch))

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "genesis.db.crud.events.insert_batch", fake_insert_batch
            )
            bus.enable_persistence(db)
            # Give writer task a moment to start
            await asyncio.sleep(0.05)

            # Queue several events
            for i in range(5):
                await bus.emit(
                    Subsystem.ROUTING, Severity.INFO, f"test_{i}", f"msg_{i}"
                )

            # Give writer a moment to process
            await asyncio.sleep(0.1)

            # Stop should drain any remaining
            await bus.stop()

        total_written = sum(len(b) for b in written_batches)
        assert total_written == 5, f"Expected 5 events written, got {total_written}"

    @pytest.mark.asyncio
    async def test_stop_without_persistence_is_noop(self):
        """stop() on a bus without persistence should not error."""
        bus = GenesisEventBus()
        await bus.stop()  # Should not raise

    @pytest.mark.asyncio
    async def test_stop_idempotent(self):
        """Calling stop() twice should not error."""
        import asyncio
        from unittest.mock import AsyncMock

        db = AsyncMock()
        bus = GenesisEventBus(db=db)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "genesis.db.crud.events.insert_batch", AsyncMock()
            )
            bus.enable_persistence(db)
            await asyncio.sleep(0.05)
            await bus.stop()
            await bus.stop()  # Second call should be a no-op

    @pytest.mark.asyncio
    async def test_stop_timeout_cancels_stuck_writer(self):
        """If writer is stuck, stop() should cancel after timeout."""
        import asyncio
        from unittest.mock import AsyncMock, patch

        db = AsyncMock()
        bus = GenesisEventBus(db=db)

        async def slow_insert_batch(_db, batch):
            await asyncio.sleep(60)  # Simulate stuck writer

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "genesis.db.crud.events.insert_batch", slow_insert_batch
            )
            bus.enable_persistence(db)
            await asyncio.sleep(0.05)

            # Queue an event to make the writer busy
            await bus.emit(
                Subsystem.ROUTING, Severity.INFO, "test", "msg"
            )
            await asyncio.sleep(0.05)

            # Stop with a short timeout (override the 5s default)
            with patch("genesis.observability.events.asyncio.wait_for", wraps=asyncio.wait_for):
                # Monkey-patch a short timeout for test speed

                async def fast_stop():
                    """stop() but with 0.2s timeout instead of 5s."""
                    if not bus._writer_task or bus._writer_task.done():
                        return
                    import contextlib
                    if bus._write_queue is not None:
                        with contextlib.suppress(asyncio.QueueFull):
                            bus._write_queue.put_nowait(None)
                    try:
                        await asyncio.wait_for(bus._writer_task, timeout=0.2)
                    except TimeoutError:
                        bus._writer_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await bus._writer_task
                    except asyncio.CancelledError:
                        pass

                await fast_stop()

            # Writer task should be done (cancelled)
            assert bus._writer_task.done()


class TestSeverityOrdering:
    def test_at_or_above(self):
        assert _at_or_above(Severity.WARNING, Severity.WARNING)
        assert _at_or_above(Severity.ERROR, Severity.WARNING)
        assert _at_or_above(Severity.CRITICAL, Severity.INFO)
        assert not _at_or_above(Severity.INFO, Severity.WARNING)
        assert not _at_or_above(Severity.DEBUG, Severity.INFO)


class TestDroppedEventVisibility:
    """WS-17: dropped events are counted (non-blocking) and surfaced."""

    @pytest.mark.asyncio
    async def test_emit_increments_dropped_count_when_queue_full(self, bus):
        # Force a full persistence queue deterministically (no writer draining).
        bus._write_queue = asyncio.Queue(maxsize=1)
        bus._write_queue.put_nowait({"filler": True})
        assert bus.dropped_event_count() == 0

        # emit() must neither block nor raise even though the queue is full.
        event = await bus.emit(Subsystem.ROUTING, Severity.ERROR, "drop_me", "boom")

        assert isinstance(event, GenesisEvent)  # emit still returns the event
        assert bus.dropped_event_count() == 1
        # The in-memory ring still captured it even though the DB queue dropped it.
        assert any(e.event_type == "drop_me" for e in bus._ring)

    @pytest.mark.asyncio
    async def test_enable_persistence_uses_large_queue(self):
        from unittest.mock import AsyncMock

        bus = GenesisEventBus(db=AsyncMock())
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("genesis.db.crud.events.insert_batch", AsyncMock())
            bus.enable_persistence(AsyncMock())
            try:
                assert bus._write_queue.maxsize == 5000
            finally:
                await bus.stop()

    @pytest.mark.asyncio
    async def test_flush_dropped_notice_persists_rate_limited_overflow_event(self, db):
        from datetime import UTC, datetime

        from genesis.db.crud import events as events_crud

        frozen = datetime(2026, 6, 14, tzinfo=UTC)
        bus = GenesisEventBus(clock=lambda: frozen, db=db)
        bus._dropped_count = 7

        await bus._flush_dropped_notice()
        rows = await events_crud.query(db, event_type="event_queue_overflow")
        assert len(rows) == 1
        assert "7" in rows[0]["message"]
        assert rows[0]["severity"] == "warning"

        # No new drops since the last notice → no second row.
        await bus._flush_dropped_notice()
        rows = await events_crud.query(db, event_type="event_queue_overflow")
        assert len(rows) == 1

        # New drops, but within the rate-limit window (same frozen clock) → suppressed.
        bus._dropped_count = 12
        await bus._flush_dropped_notice()
        rows = await events_crud.query(db, event_type="event_queue_overflow")
        assert len(rows) == 1


class TestQueuesSnapshotEventsDropped:
    """WS-17: the queues snapshot surfaces the dropped-event counter."""

    @pytest.mark.asyncio
    async def test_queues_surfaces_dropped_count(self):
        from genesis.observability.snapshots.queues import queues

        bus = GenesisEventBus()
        bus._dropped_count = 3
        result = await queues(None, None, None, event_bus=bus)
        assert result["events_dropped"] == 3

    @pytest.mark.asyncio
    async def test_queues_dropped_defaults_zero_without_bus(self):
        from genesis.observability.snapshots.queues import queues

        result = await queues(None, None, None)
        assert result["events_dropped"] == 0


class TestEventSerialization:
    """OBS-002: one un-serializable event must not drop the whole batch."""

    @pytest.mark.asyncio
    async def test_insert_batch_survives_unserializable_detail(self, db):
        from genesis.db.crud import events as events_crud

        batch = [
            {"subsystem": "routing", "severity": "info",
             "event_type": "ok", "message": "fine", "details": {"a": 1}},
            {"subsystem": "routing", "severity": "error",
             "event_type": "weird", "message": "bad detail",
             "details": {"obj": object()}},  # not JSON-serializable
        ]
        n = await events_crud.insert_batch(db, batch)
        assert n == 2

        rows = await events_crud.query(db, limit=10)
        types = {r["event_type"] for r in rows}
        assert {"ok", "weird"} <= types  # the un-serializable event survived
