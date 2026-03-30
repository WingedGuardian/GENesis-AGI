"""Idle detection — tracks user activity to identify surplus compute windows."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta


class IdleDetector:
    """Timer-based idle detection.

    Tracks last user interaction. If no activity for threshold_minutes,
    the system is considered idle and surplus tasks can run.
    """

    def __init__(self, *, clock=None):
        self._clock = clock or (lambda: datetime.now(UTC))
        self._last_activity_at: datetime | None = None

    def mark_active(self) -> None:
        """Record a user interaction. Resets the idle timer."""
        self._last_activity_at = self._clock()

    def is_idle(self, *, threshold_minutes: int = 15) -> bool:
        """Check if the system has been idle long enough for surplus work."""
        if self._last_activity_at is None:
            return True
        elapsed = self._clock() - self._last_activity_at
        return elapsed >= timedelta(minutes=threshold_minutes)

    def idle_since(self, *, threshold_minutes: int = 15) -> datetime | None:
        """Return the timestamp when idle started, or None if not idle."""
        if self._last_activity_at is None:
            return None
        if not self.is_idle(threshold_minutes=threshold_minutes):
            return None
        return self._last_activity_at
