"""CCBudgetTracker — tracks CC session usage and determines throttling status."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import aiosqlite

from genesis.resilience.state import CCStatus

logger = logging.getLogger(__name__)

# Priority levels for CC sessions
P_FOREGROUND = 0
P_URGENT = 1
P_REFLECTION = 2
P_SCHEDULED = 3
P_BACKGROUND = 4
P_SURPLUS = 5


class CCBudgetTracker:
    """Tracks CC session usage and determines throttling status."""

    def __init__(
        self,
        db: aiosqlite.Connection,
        *,
        max_sessions_per_hour: int = 20,
        throttle_threshold_pct: float = 0.80,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._db = db
        self._max = max_sessions_per_hour
        self._threshold = throttle_threshold_pct
        self._clock = clock or (lambda: datetime.now(UTC))

    async def record_session_start(self, session_type: str, priority: int) -> None:
        """Record that a CC session was started (writes to cc_sessions)."""
        import uuid

        now = self._clock().isoformat()
        try:
            await self._db.execute(
                """INSERT INTO cc_sessions
                   (id, session_type, model, started_at, last_activity_at, status, source_tag)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (str(uuid.uuid4()), session_type, "sonnet", now, now, "active", f"priority_{priority}"),
            )
            await self._db.commit()
        except Exception:
            logger.error(
                "CC budget record FAILED: session_type=%s priority=%d",
                session_type, priority, exc_info=True,
            )

    async def _count_recent_sessions(self) -> int:
        """Count sessions started in the last hour (active or completed)."""
        cutoff = (self._clock() - timedelta(hours=1)).isoformat()
        cursor = await self._db.execute(
            """SELECT COUNT(*) FROM cc_sessions
               WHERE started_at > ? AND status IN ('active', 'completed', 'expired')""",
            (cutoff,),
        )
        row = await cursor.fetchone()
        return int(row[0])

    async def get_usage_pct(self) -> float:
        """Return session usage as fraction 0.0-1.0."""
        count = await self._count_recent_sessions()
        return count / self._max if self._max > 0 else 0.0

    async def should_throttle(self, requested_priority: int) -> bool:
        """Return True if this priority level should be throttled."""
        if requested_priority == P_FOREGROUND:
            return False

        status = await self.get_status()
        if status == CCStatus.RATE_LIMITED:
            return requested_priority >= P_REFLECTION
        if status == CCStatus.THROTTLED:
            return requested_priority >= P_BACKGROUND
        return False

    async def get_status(self) -> CCStatus:
        """Compute current CC status from usage."""
        usage = await self.get_usage_pct()
        if usage >= 1.0:
            return CCStatus.RATE_LIMITED
        if usage >= self._threshold:
            return CCStatus.THROTTLED
        return CCStatus.NORMAL
