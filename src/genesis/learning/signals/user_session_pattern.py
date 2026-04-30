"""UserSessionPatternCollector — signal for user activity rhythm deviation.

Tracks the user's typical foreground session pattern from cc_sessions
and reports how much current activity deviates from their baseline.

Not an alarm — a deviation could mean vacation, schedule change, or just
a quiet day. The signal provides data; the ego interprets with context.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import aiosqlite

from genesis.awareness.types import SignalReading

logger = logging.getLogger(__name__)

# How many days of history to use for baseline
_BASELINE_WINDOW_DAYS = 14
# How many recent days to compare against baseline
_RECENT_WINDOW_DAYS = 2


class UserSessionPatternCollector:
    """Reports deviation from the user's typical session activity.

    Signal value:
      0.0 = activity matches baseline pattern (or no baseline yet)
      0.5 = noticeably different from typical (e.g., half the usual sessions)
      1.0 = significantly unusual (absent when normally active, or vice versa)
    """

    signal_name = "user_session_pattern"

    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    async def collect(self) -> SignalReading:
        now = datetime.now(UTC)

        baseline_avg = await self._baseline_daily_sessions(now)
        recent_avg = await self._recent_daily_sessions(now)

        if baseline_avg is None:
            # Not enough history — no deviation to report
            return SignalReading(
                name=self.signal_name,
                value=0.0,
                source="cc_sessions",
                collected_at=now.isoformat(),
                baseline_note=(
                    "0.0=typical activity or insufficient history. "
                    "0.5=noticeably different from usual. "
                    "1.0=significantly unusual activity pattern."
                ),
            )

        # Calculate deviation ratio
        if baseline_avg < 0.1:
            # User rarely has foreground sessions — any activity is normal
            deviation = 0.0
        else:
            # How far is recent from baseline? Symmetric: both drops and
            # surges register, but drops matter more for staleness detection.
            ratio = recent_avg / baseline_avg
            if 0.5 <= ratio <= 1.5:
                # Within 50% of normal — low deviation
                deviation = abs(1.0 - ratio) * 0.5
            else:
                # More than 50% off baseline
                deviation = min(1.0, abs(1.0 - ratio) * 0.7)

        return SignalReading(
            name=self.signal_name,
            value=round(deviation, 3),
            source="cc_sessions",
            collected_at=now.isoformat(),
            baseline_note=(
                f"Baseline: {baseline_avg:.1f} sessions/day "
                f"(last {_BASELINE_WINDOW_DAYS}d). "
                f"Recent: {recent_avg:.1f}/day (last {_RECENT_WINDOW_DAYS}d). "
                "0.0=typical, 1.0=very unusual."
            ),
        )

    async def _baseline_daily_sessions(self, now: datetime) -> float | None:
        """Average daily foreground sessions over the baseline window."""
        cutoff = (now - timedelta(days=_BASELINE_WINDOW_DAYS)).isoformat()
        recent_cutoff = (now - timedelta(days=_RECENT_WINDOW_DAYS)).isoformat()
        try:
            cursor = await self._db.execute(
                "SELECT COUNT(*) FROM cc_sessions "
                "WHERE source_tag = 'foreground' "
                "AND started_at >= ? "
                "AND started_at < ?",
                (cutoff, recent_cutoff),
            )
            row = await cursor.fetchone()
        except Exception:
            logger.debug("UserSessionPatternCollector: baseline query failed", exc_info=True)
            return None

        count = row[0] if row else 0
        window_days = _BASELINE_WINDOW_DAYS - _RECENT_WINDOW_DAYS
        if window_days <= 0 or count < 3:
            # Need at least 3 sessions to establish a baseline
            return None

        return count / window_days

    async def _recent_daily_sessions(self, now: datetime) -> float:
        """Average daily foreground sessions in the recent window."""
        cutoff = (now - timedelta(days=_RECENT_WINDOW_DAYS)).isoformat()
        try:
            cursor = await self._db.execute(
                "SELECT COUNT(*) FROM cc_sessions "
                "WHERE source_tag = 'foreground' "
                "AND started_at >= ?",
                (cutoff,),
            )
            row = await cursor.fetchone()
        except Exception:
            logger.debug("UserSessionPatternCollector: recent query failed", exc_info=True)
            return 0.0

        count = row[0] if row else 0
        return count / _RECENT_WINDOW_DAYS
