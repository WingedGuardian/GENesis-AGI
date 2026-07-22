"""Reflex ingestion — task.failed events → fingerprinted signal rows.

Two-stage by design (mirrors the event bus's own persistence path): the
bus subscriber only ENQUEUES — the event bus dispatches listeners inline
inside ``emit()``, so a DB commit there would serialize every ERROR+
event behind SQLite contention and compound a failure burst instead of
absorbing it. A dedicated worker (itself a ``tracked_task``) drains the
bounded queue and upserts. Overflow drops are counted, never silent.

Failure containment: the subscriber body and the worker's per-item
processing are each fully guarded — reflex ingestion must never break the
bus or die to one poison item. If the worker task itself crashes, that IS
a ``task.failed`` signal (emitted via its own tracked_task wrapper); the
queue then fills and drops — no recursion, no loop.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from genesis.db.crud import reflex_signals as signals_crud
from genesis.reflex.config import load_reflex_config
from genesis.reflex.fingerprint import class_key, derive_subsystem, fingerprint

if TYPE_CHECKING:
    from collections.abc import Callable

    import aiosqlite

    from genesis.observability.types import GenesisEvent
    from genesis.reflex.config import ReflexConfig

logger = logging.getLogger(__name__)

_QUEUE_SIZE = 1000
# Config re-read cadence: turning ingestion OFF (config or env kill) takes
# effect within this window without a restart — the runtime brake.
_REFRESH_INTERVAL_S = 30.0
_DROP_WARN_EVERY = 100


class ReflexIngestor:
    """Subscribe-enqueue-drain pipeline from the event bus into reflex_signals."""

    def __init__(
        self,
        db: aiosqlite.Connection,
        *,
        config_loader: Callable[[], ReflexConfig] = load_reflex_config,
        queue_size: int = _QUEUE_SIZE,
        refresh_interval_s: float = _REFRESH_INTERVAL_S,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._db = db
        self._config_loader = config_loader
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=queue_size)
        self._refresh_interval_s = refresh_interval_s
        self._clock = clock or (lambda: datetime.now(UTC))
        self._enabled = False
        self._last_refresh = 0.0
        self._dropped = 0
        self._processed = 0
        self._worker_task: asyncio.Task | None = None

    # ── bus side (runs inside emit() — fast, never raises) ──────────────

    async def handle_event(self, event: GenesisEvent) -> None:
        try:
            if event.event_type != "task.failed" or not self._enabled:
                return
            details = getattr(event, "details", None) or {}
            payload = {
                "task_name": str(details.get("task_name") or "unnamed"),
                "error": str(details.get("error") or ""),
                "error_type": str(details.get("error_type") or "UnknownError"),
                "error_frames": [str(f) for f in (details.get("error_frames") or [])],
                "subsystem": str(getattr(event, "subsystem", "") or "health"),
            }
            try:
                self._queue.put_nowait(payload)
            except asyncio.QueueFull:
                self._dropped += 1
                if self._dropped % _DROP_WARN_EVERY == 1:
                    logger.warning(
                        "Reflex ingest queue full — dropped %d task.failed events so far",
                        self._dropped,
                    )
        except Exception:  # never break the bus — reflex is an observer
            logger.warning("Reflex ingest handler error (event dropped)", exc_info=True)

    # ── worker side (off the dispatch path) ─────────────────────────────

    def start(self, event_bus: Any) -> None:
        """Subscribe to the bus and start the drain worker."""
        from genesis.observability.types import Severity
        from genesis.util.tasks import tracked_task

        self.refresh_enabled()
        event_bus.subscribe(self.handle_event, min_severity=Severity.ERROR)
        self._worker_task = tracked_task(
            self._worker(),
            name="reflex-ingest-worker",
            event_bus=event_bus,
        )
        logger.info(
            "Reflex ingestion started (enabled=%s, queue=%d)",
            self._enabled,
            self._queue.maxsize,
        )

    def refresh_enabled(self) -> None:
        """Re-read config (incl. env kill). OFF takes effect without restart."""
        try:
            self._enabled = self._config_loader().ingest_enabled
        except Exception:
            logger.warning(
                "Reflex config refresh failed — keeping enabled=%s", self._enabled, exc_info=True
            )
        self._last_refresh = time.monotonic()

    async def _worker(self) -> None:
        while True:
            try:
                item = await asyncio.wait_for(self._queue.get(), timeout=self._refresh_interval_s)
            except TimeoutError:
                self.refresh_enabled()
                continue
            try:
                await self.process(item)
                self._processed += 1
            except Exception:  # one poison item must not kill the drain
                logger.error("Reflex ingest failed to process item: %s", item, exc_info=True)
            if time.monotonic() - self._last_refresh > self._refresh_interval_s:
                self.refresh_enabled()

    async def process(self, item: dict[str, Any]) -> dict:
        """Fingerprint + upsert one task.failed payload; apply reopen policy."""
        frames = item["error_frames"]
        error_type = item["error_type"]
        task_name = item["task_name"]
        subsystem = derive_subsystem(frames, item["subsystem"])
        fp = fingerprint(task_name, error_type, frames)
        now = self._clock().isoformat()
        row = await signals_crud.upsert_occurrence(
            self._db,
            fingerprint=fp,
            class_key=class_key(error_type, subsystem),
            task_name=task_name,
            subsystem=subsystem,
            error_type=error_type,
            error_message=item["error"][:500] or None,
            traceback_tail=">".join(frames) or None,
            now=now,
        )
        # Recurrence of a terminal signal past its mute window → back to
        # 'new' (for a merged signal: evidence the fix did not hold).
        # Self-guarded — a no-op for active or muted signals.
        if await signals_crud.maybe_reopen(self._db, fingerprint=fp, now=now):
            row = await signals_crud.get_by_fingerprint(self._db, fp) or row
        return row

    @property
    def stats(self) -> dict[str, int | bool]:
        return {
            "enabled": self._enabled,
            "queued": self._queue.qsize(),
            "processed": self._processed,
            "dropped": self._dropped,
        }
