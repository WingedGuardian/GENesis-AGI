"""GenesisEventBus — listener-based event dispatch with stdlib logging."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from genesis.observability.session_context import get_session_id as _get_context_session_id
from genesis.observability.types import GenesisEvent, Severity, Subsystem

logger = logging.getLogger(__name__)

# Listener signature: async (event) -> None
Listener = Callable[[GenesisEvent], Awaitable[None]]

# WS-17: minimum spacing between persisted "events dropped" overflow notices,
# so an incident storm produces one visible meta-event rather than thousands.
_DROP_NOTICE_MIN_INTERVAL_S = 60.0


class GenesisEventBus:
    """Lightweight async event bus for Genesis observability.

    Dispatches events inline (await) to registered listeners.
    Also logs every event via stdlib logging at the matching severity level.
    Optionally persists events to the DB via a background write queue.
    """

    def __init__(self, *, clock=None, ring_size: int = 200, db=None):
        self._listeners: list[tuple[Severity | None, Listener]] = []
        self._clock = clock or (lambda: datetime.now(UTC))
        self._ring: deque[GenesisEvent] = deque(maxlen=ring_size)
        self._db = db
        self._write_queue: asyncio.Queue | None = None
        self._writer_task: asyncio.Task | None = None
        # WS-17: count events dropped when the persistence queue is full so the
        # silent loss is visible — surfaced on the health snapshot and via a
        # rate-limited overflow meta-event emitted by the writer task.
        self._dropped_count = 0
        self._dropped_reported = 0
        self._last_drop_notice_at: datetime | None = None

    def enable_persistence(self, db) -> None:
        """Enable DB persistence for events.  Safe to call after construction."""
        self._db = db
        # Larger buffer (WS-17): absorbs incident storms before dropping; worst
        # case ~a few MB. Drops are still counted and surfaced, never silent.
        self._write_queue = asyncio.Queue(maxsize=5000)
        # Use bare create_task here to avoid circular import with
        # genesis.util.tasks (which imports from this module's types).
        # Error observation via done callback compensates for no tracked_task().
        self._writer_task = asyncio.create_task(self._db_writer(), name="event-db-writer")

        def _on_writer_done(task: asyncio.Task) -> None:
            if task.cancelled():
                return
            exc = task.exception()
            if exc:
                logger.error("Event DB writer died unexpectedly: %s", exc, exc_info=exc)

        self._writer_task.add_done_callback(_on_writer_done)
        logger.info("Event persistence enabled")

    async def _db_writer(self) -> None:
        """Background task that drains the write queue and batch-inserts events."""
        from genesis.db.crud import events as events_crud

        while True:
            batch: list[dict] = []
            try:
                # Wait for at least one event
                item = await self._write_queue.get()
                if item is None:  # Shutdown sentinel
                    break
                batch.append(item)
                # Drain any additional queued events (up to 50 per batch)
                while not self._write_queue.empty() and len(batch) < 50:
                    next_item = self._write_queue.get_nowait()
                    if next_item is None:
                        break
                    batch.append(next_item)
                await events_crud.insert_batch(self._db, batch)
                # WS-17: off the hot path, surface any drops since last notice.
                await self._flush_dropped_notice()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.error("Event DB write failed for %d events", len(batch), exc_info=True)

        # Drain remaining queued events after shutdown signal
        remaining: list[dict] = []
        while not self._write_queue.empty():
            item = self._write_queue.get_nowait()
            if item is not None:
                remaining.append(item)
        if remaining:
            try:
                await events_crud.insert_batch(self._db, remaining)
                logger.info("Flushed %d events on shutdown", len(remaining))
            except Exception:
                logger.error("Failed to flush %d events on shutdown", len(remaining), exc_info=True)

    def dropped_event_count(self) -> int:
        """Cumulative count of events dropped because the persistence queue was
        full. Surfaced on the health snapshot as ``queues.events_dropped``."""
        return self._dropped_count

    async def _flush_dropped_notice(self) -> None:
        """Persist a single rate-limited overflow meta-event when events have
        been dropped since the last notice.

        Called from the writer task only — it owns ``self._db`` and runs off the
        hot ``emit()`` path. Writing to the DB inside ``emit()``'s QueueFull
        handler would mean a DB write exactly when the DB is most contended.
        """
        if self._db is None:
            return
        dropped = self._dropped_count
        if dropped <= self._dropped_reported:
            return
        now = self._clock()
        if (
            self._last_drop_notice_at is not None
            and (now - self._last_drop_notice_at).total_seconds()
            < _DROP_NOTICE_MIN_INTERVAL_S
        ):
            return
        new_drops = dropped - self._dropped_reported
        maxsize = self._write_queue.maxsize if self._write_queue else None
        try:
            from genesis.db.crud import events as events_crud

            await events_crud.insert(
                self._db,
                subsystem=Subsystem.OBSERVABILITY.value,
                severity=Severity.WARNING.value,
                event_type="event_queue_overflow",
                message=(
                    f"Event persistence queue full — dropped {new_drops} "
                    f"event(s) (total {dropped})"
                ),
                details={
                    "dropped_new": new_drops,
                    "dropped_total": dropped,
                    "queue_maxsize": maxsize,
                },
                timestamp=now.isoformat(),
            )
        except Exception:
            logger.debug("Failed to persist event-overflow notice", exc_info=True)
            return
        self._dropped_reported = dropped
        self._last_drop_notice_at = now

    async def stop(self) -> None:
        """Stop the background writer task, draining queued events first."""
        if not self._writer_task or self._writer_task.done():
            return
        # Send sentinel to wake blocked get() and trigger clean shutdown
        if self._write_queue is not None:
            with contextlib.suppress(asyncio.QueueFull):
                self._write_queue.put_nowait(None)
        try:
            await asyncio.wait_for(self._writer_task, timeout=5.0)
        except TimeoutError:
            logger.warning("Event writer did not drain within 5s; cancelling")
            self._writer_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._writer_task
        except asyncio.CancelledError:
            pass

    def subscribe(
        self, listener: Listener, *, min_severity: Severity | None = None
    ) -> None:
        """Register a listener. If min_severity is set, only events at or above it are dispatched."""
        self._listeners.append((min_severity, listener))

    async def emit(
        self,
        subsystem: Subsystem,
        severity: Severity,
        event_type: str,
        message: str,
        **details,
    ) -> GenesisEvent:
        """Emit an event: log it and dispatch to all matching listeners."""
        event = GenesisEvent(
            subsystem=subsystem,
            severity=severity,
            event_type=event_type,
            message=message,
            timestamp=self._clock().isoformat(),
            details=details,
        )

        self._ring.append(event)

        # Queue for DB persistence (fire-and-forget)
        if self._write_queue is not None:
            try:
                self._write_queue.put_nowait({
                    "id": str(uuid.uuid4()),
                    "timestamp": event.timestamp,
                    "subsystem": subsystem.value if hasattr(subsystem, "value") else str(subsystem),
                    "severity": severity.value if hasattr(severity, "value") else str(severity),
                    "event_type": event_type,
                    "message": message,
                    "details": details or None,
                    "session_id": (
                        sid if (sid := details.get("session_id")) is not None
                        else _get_context_session_id()
                    ) if details else _get_context_session_id(),
                })
            except asyncio.QueueFull:
                # WS-17: count the drop (non-blocking) instead of losing it
                # silently; the writer task surfaces it as a meta-event.
                self._dropped_count += 1
                logger.warning("Event write queue full — dropping event: %s/%s", event_type, message[:80])

        # Log via stdlib at the matching level
        log_level = _severity_to_log_level(severity)
        logger.log(
            log_level,
            "subsystem=%s event=%s msg=%s",
            subsystem.value if hasattr(subsystem, "value") else str(subsystem),
            event_type,
            message,
        )

        # Dispatch to listeners
        for min_sev, listener in self._listeners:
            if min_sev is not None and not _at_or_above(severity, min_sev):
                continue
            try:
                await listener(event)
            except Exception:
                logger.exception(
                    "Listener %s failed for event %s",
                    getattr(listener, "__name__", repr(listener)),
                    event_type,
                )

        return event

    def recent_events(
        self,
        *,
        min_severity: Severity | None = None,
        subsystem: Subsystem | None = None,
        limit: int = 50,
    ) -> list[GenesisEvent]:
        """Return recent events from the ring buffer, newest first."""
        results = []
        for event in reversed(self._ring):
            if min_severity and not _at_or_above(event.severity, min_severity):
                continue
            if subsystem and event.subsystem != subsystem:
                continue
            results.append(event)
            if len(results) >= limit:
                break
        return results


_SEVERITY_ORDER = [
    Severity.DEBUG,
    Severity.INFO,
    Severity.WARNING,
    Severity.ERROR,
    Severity.CRITICAL,
]


def _at_or_above(severity: Severity, minimum: Severity) -> bool:
    return _SEVERITY_ORDER.index(severity) >= _SEVERITY_ORDER.index(minimum)


def _severity_to_log_level(severity: Severity) -> int:
    return {
        Severity.DEBUG: logging.DEBUG,
        Severity.INFO: logging.INFO,
        Severity.WARNING: logging.WARNING,
        Severity.ERROR: logging.ERROR,
        Severity.CRITICAL: logging.CRITICAL,
    }[severity]
