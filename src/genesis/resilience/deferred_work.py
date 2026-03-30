"""DeferredWorkQueue — high-level interface for deferring and draining work items."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

import aiosqlite

from genesis.db.crud import deferred_work as crud

logger = logging.getLogger(__name__)

# ── Priority constants ───────────────────────────────────────────────────────

FOREGROUND = 10
URGENT_OUTREACH = 20
REFLECTION = 30
SCHEDULED = 40
MEMORY_OPS = 50
OUTREACH_DRAFT = 60
MORNING_REPORT = 70
SURPLUS = 80

# ── Staleness policy constants ───────────────────────────────────────────────

DRAIN = "drain"
REFRESH = "refresh"
DISCARD = "discard"
TTL = "ttl"


class DeferredWorkQueue:
    """Queue for work items deferred due to degraded resilience state."""

    def __init__(
        self,
        db: aiosqlite.Connection,
        event_bus=None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._db = db
        self._event_bus = event_bus
        self._clock = clock or (lambda: datetime.now(UTC))

    async def enqueue(
        self,
        work_type: str,
        call_site_id: str | None,
        priority: int,
        payload: str,
        reason: str,
        staleness_policy: str = DRAIN,
        staleness_ttl_s: int | None = None,
    ) -> str:
        """Enqueue a deferred work item. Returns the item ID."""
        now = self._clock().isoformat()
        item_id = str(uuid.uuid4())
        try:
            await crud.create(
                self._db,
                id=item_id,
                work_type=work_type,
                call_site_id=call_site_id,
                priority=priority,
                payload_json=payload,
                deferred_at=now,
                deferred_reason=reason,
                staleness_policy=staleness_policy,
                staleness_ttl_s=staleness_ttl_s,
                created_at=now,
            )
        except Exception:
            logger.error(
                "Deferred work enqueue FAILED — work lost: type=%s priority=%d reason=%s",
                work_type, priority, reason, exc_info=True,
            )
            return None
        logger.info(
            "Deferred work enqueued: type=%s priority=%d reason=%s id=%s",
            work_type, priority, reason, item_id,
        )
        return item_id

    async def next_pending(self, max_priority: int = 100) -> dict | None:
        """Return the highest-priority pending item, or None."""
        items = await crud.query_pending(
            self._db, max_priority=max_priority, limit=1,
        )
        return items[0] if items else None

    async def mark_processing(self, id: str) -> bool:
        """Mark an item as being processed."""
        now = self._clock().isoformat()
        return await crud.update_status(
            self._db, id, status="processing", last_attempt_at=now,
        )

    async def mark_completed(self, id: str) -> bool:
        """Mark an item as completed."""
        now = self._clock().isoformat()
        return await crud.update_status(
            self._db, id, status="completed", completed_at=now,
        )

    async def mark_discarded(self, id: str, reason: str) -> bool:
        """Mark an item as discarded."""
        now = self._clock().isoformat()
        return await crud.update_status(
            self._db, id, status="discarded", error_message=reason, completed_at=now,
        )

    async def expire_stuck_processing(self, max_age_hours: int = 2) -> int:
        """Reset items stuck in 'processing' back to 'pending'.

        Items get orphaned when the process is killed mid-execution
        (e.g., bridge restart). Returns count reset.
        """
        count = await crud.expire_stuck_processing(
            self._db, max_age_hours=max_age_hours,
        )
        if count > 0:
            logger.info(
                "Reset %d stuck processing items to pending (age > %dh)",
                count, max_age_hours,
            )
        return count

    async def expire_stale(self) -> int:
        """Apply staleness policies and expire stale items. Returns count expired."""
        now = self._clock().isoformat()
        count = await crud.expire_by_policy(self._db, now_iso=now)
        if count > 0:
            logger.info("Expired %d stale deferred work items", count)
        return count

    async def reset_to_pending(self, id: str) -> bool:
        """Reset an item back to pending after a failed processing attempt."""
        return await crud.update_status(self._db, id, status="pending")

    async def count_pending(self, work_type: str | None = None) -> int:
        """Count pending items, optionally filtered by work_type."""
        return await crud.count_pending(self._db, work_type=work_type)

    async def drain_by_priority(self, limit: int = 10) -> list[dict]:
        """Return up to `limit` pending items ordered by priority."""
        return await crud.drain_by_priority(self._db, limit=limit)
