"""UserGoalStalenessCollector — signal for stale user goals and projects.

Scans follow-ups with strategy='user_input_needed' and user_model_cache
active_projects for goals that haven't seen progress.

This is observational only — it reports staleness so the ego can decide
what (if anything) to do. It does NOT recommend action.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

import aiosqlite

from genesis.awareness.types import SignalReading

logger = logging.getLogger(__name__)

# Goals older than this are considered stale
_STALE_THRESHOLD_DAYS = 7
# Full staleness at this many days
_MAX_STALE_DAYS = 14


class UserGoalStalenessCollector:
    """Reports how stale the user's most neglected goal/follow-up is.

    Signal value:
      0.0 = all goals fresh (recent activity or no goals tracked)
      0.5 = goals aging past 7 days without user engagement
      1.0 = 14+ days stale
    """

    signal_name = "user_goal_staleness"

    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    async def collect(self) -> SignalReading:
        now = datetime.now(UTC)

        # Source 1: follow-ups needing user input
        follow_up_staleness = await self._check_follow_ups(now)

        # Source 2: active_projects from user_model_cache
        project_staleness = await self._check_active_projects(now)

        # Take the worse (higher) staleness
        value = max(follow_up_staleness, project_staleness)

        return SignalReading(
            name=self.signal_name,
            value=round(value, 3),
            source="follow_ups+user_model",
            collected_at=now.isoformat(),
            baseline_note=(
                "0.0=fresh or none tracked. 0.5=7+ days stale. "
                "1.0=14+ days stale (EXPECTED when old follow-ups "
                "exist — not a system anomaly, not noteworthy unless "
                "it changed sharply from a prior tick)."
            ),
        )

    async def _check_follow_ups(self, now: datetime) -> float:
        """Check follow-ups with strategy='user_input_needed' for staleness."""
        try:
            cursor = await self._db.execute(
                "SELECT created_at FROM follow_ups "
                "WHERE strategy = 'user_input_needed' "
                "AND status IN ('pending', 'blocked') "
                "ORDER BY created_at ASC LIMIT 1"
            )
            row = await cursor.fetchone()
        except Exception:
            logger.debug("UserGoalStalenessCollector: follow_ups query failed", exc_info=True)
            return 0.0

        if not row:
            return 0.0

        try:
            created_at = datetime.fromisoformat(row[0])
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=UTC)
        except (ValueError, TypeError):
            return 0.0

        age_days = (now - created_at).total_seconds() / 86400
        return self._days_to_signal(age_days)

    async def _check_active_projects(self, now: datetime) -> float:
        """Check user_model_cache for active_projects and their freshness."""
        try:
            cursor = await self._db.execute(
                "SELECT model_json, synthesized_at FROM user_model_cache "
                "WHERE id = 'current'"
            )
            row = await cursor.fetchone()
        except Exception:
            logger.debug("UserGoalStalenessCollector: user_model query failed", exc_info=True)
            return 0.0

        if not row:
            return 0.0

        model_json, synthesized_at = row
        try:
            model = json.loads(model_json) if model_json else {}
        except (json.JSONDecodeError, TypeError):
            return 0.0

        projects = model.get("active_projects")
        if not projects:
            return 0.0

        # Use synthesized_at as proxy for last model update
        try:
            synth_dt = datetime.fromisoformat(synthesized_at)
            if synth_dt.tzinfo is None:
                synth_dt = synth_dt.replace(tzinfo=UTC)
        except (ValueError, TypeError):
            return 0.0

        age_days = (now - synth_dt).total_seconds() / 86400
        # Only flag if the model itself is stale — if it was recently
        # synthesized, the projects are presumably current
        return self._days_to_signal(age_days)

    @staticmethod
    def _days_to_signal(age_days: float) -> float:
        """Convert age in days to 0.0-1.0 signal value."""
        if age_days < _STALE_THRESHOLD_DAYS:
            return 0.0
        if age_days >= _MAX_STALE_DAYS:
            return 1.0
        # Linear interpolation between threshold and max
        return (age_days - _STALE_THRESHOLD_DAYS) / (_MAX_STALE_DAYS - _STALE_THRESHOLD_DAYS)
