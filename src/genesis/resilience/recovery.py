"""RecoveryOrchestrator — coordinates recovery after resilience state improves."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from genesis.observability.types import ProbeStatus, Severity, Subsystem

logger = logging.getLogger(__name__)


@dataclass
class RecoveryReport:
    items_drained: int = 0
    items_expired: int = 0
    items_unstuck: int = 0
    items_failed: int = 0
    embeddings_recovered: int = 0
    dead_letters_replayed: int = 0
    dead_letters_failed: int = 0
    duration_s: float = 0.0


class RecoveryOrchestrator:
    def __init__(
        self,
        *,
        db,
        state_machine,
        deferred_queue,
        embedding_worker,
        dead_letter,
        health_probes=None,
        event_bus=None,
        confirmation_probes: int = 3,
        confirmation_interval_s: float = 30,
        drain_pace_s: float = 30,
        queue_overflow_threshold: int = 1000,
    ) -> None:
        self._db = db
        self._state_machine = state_machine
        self._deferred_queue = deferred_queue
        self._embedding_worker = embedding_worker
        self._dead_letter = dead_letter
        self._dispatch_fn = None
        self._health_probes = health_probes or []
        self._event_bus = event_bus
        self._confirmation_probes = confirmation_probes
        self._confirmation_interval_s = confirmation_interval_s
        self._drain_pace_s = drain_pace_s
        self._queue_overflow_threshold = queue_overflow_threshold

    def set_dispatch_fn(self, fn) -> None:
        """Set the callback for dead letter re-dispatch.

        fn(call_site_id, messages) -> RoutingResult
        """
        self._dispatch_fn = fn

    async def should_recover(self, old_state, new_state) -> bool:
        """Return True if any axis improved (state got better)."""
        for axis in ("cloud", "memory", "embedding", "cc"):
            old_val = getattr(old_state, axis)
            new_val = getattr(new_state, axis)
            if int(new_val) > int(old_val):  # Higher = better
                return True
        return False

    async def confirm_recovery(self) -> bool:
        """Run N consecutive health probes to confirm recovery is stable."""
        if not self._health_probes:
            return True

        for i in range(self._confirmation_probes):
            for probe in self._health_probes:
                result = await probe()
                if result.status != ProbeStatus.HEALTHY:
                    logger.info(
                        "Recovery confirmation failed on probe %s (round %d): %s",
                        result.name, i + 1, result.message,
                    )
                    return False
            if i < self._confirmation_probes - 1:
                await asyncio.sleep(self._confirmation_interval_s)

        return True

    async def run_recovery(self) -> RecoveryReport:
        """Execute recovery: expire stale -> drain embeddings -> drain work -> replay dead letters."""
        start = time.monotonic()
        report = RecoveryReport()

        # 0. Reset items orphaned in 'processing' by process kills
        report.items_unstuck = await self._deferred_queue.expire_stuck_processing()

        # 1. Expire stale deferred work
        report.items_expired = await self._deferred_queue.expire_stale()

        # 1b. Reset failed embeddings to pending so they can be retried
        if self._db is not None:
            try:
                from genesis.db.crud import pending_embeddings
                reset = await pending_embeddings.reset_failed_to_pending(self._db)
                if reset > 0:
                    logger.info("Reset %d failed embeddings to pending for retry", reset)
            except Exception:
                logger.warning("Failed to reset failed embeddings", exc_info=True)

        # 2. Drain pending embeddings — limit=500 to handle full extraction
        # cycle output (extraction queues FTS5-only, recovery embeds at pace)
        report.embeddings_recovered = await self._embedding_worker.drain_pending(limit=500)

        # 3. Drain deferred work by priority
        items = await self._deferred_queue.drain_by_priority(limit=50)
        for item in items:
            try:
                await self._deferred_queue.mark_processing(item["id"])
                # Actual re-dispatch is Phase 8/9 work — just mark completed
                await self._deferred_queue.mark_completed(item["id"])
                report.items_drained += 1
            except Exception:
                report.items_failed += 1
                logger.exception("Failed to process deferred item %s", item["id"])

        # 4. Re-dispatch dead letters (or mark-only if no dispatch_fn)
        if self._dispatch_fn is not None and hasattr(self._dead_letter, "redispatch"):
            succeeded, failed = await self._dead_letter.redispatch(self._dispatch_fn)
            report.dead_letters_replayed = succeeded
            report.dead_letters_failed = failed
        else:
            report.dead_letters_replayed = await self._dead_letter.replay_pending(
                target_provider="all",
            )

        # Check queue overflow
        pending_count = await self._deferred_queue.count_pending()
        if pending_count > self._queue_overflow_threshold and self._event_bus:
            await self._event_bus.emit(
                Subsystem.HEALTH,
                Severity.WARNING,
                "recovery.queue_overflow",
                f"Deferred work queue has {pending_count} items (threshold: {self._queue_overflow_threshold})",
                pending_count=pending_count,
                threshold=self._queue_overflow_threshold,
            )

        report.duration_s = time.monotonic() - start
        logger.info(
            "Recovery completed: drained=%d expired=%d embeddings=%d dead_letters=%d (%.1fs)",
            report.items_drained, report.items_expired,
            report.embeddings_recovered, report.dead_letters_replayed,
            report.duration_s,
        )
        return report
