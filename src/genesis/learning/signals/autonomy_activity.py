"""AutonomyActivityCollector — reports autonomy state transitions as awareness signal."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import aiosqlite

from genesis.awareness.types import SignalReading
from genesis.db.crud import autonomy

logger = logging.getLogger(__name__)


class AutonomyActivityCollector:
    """Reports autonomy transition state as a 0.0–1.0 signal.

    | Condition                                      | Signal Value |
    |-----------------------------------------------|-------------|
    | Stable (no corrections, no level gaps)        | 0.0         |
    | Corrections accumulating (consecutive > 0)    | 0.3         |
    | Near regression (consecutive >= threshold-1)  | 0.7         |
    | Regression, earn-back proposal pending        | 0.7         |
    | Regression active, no earn-back path pending  | 1.0         |

    Checks ALL autonomy categories — the worst state drives the signal.
    A regression with a pending ``autonomy_earnback`` proposal is dampened
    to 0.7: the system has already surfaced the recovery path and is
    waiting on the user, so a pinned 1.0 would be pure reflection noise.
    """

    signal_name = "autonomy_activity"
    _REGRESSION_THRESHOLD = 3  # consecutive corrections before regression likely

    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    async def collect(self) -> SignalReading:
        try:
            categories = await autonomy.list_all(self._db)
        except Exception:
            logger.error("AutonomyActivityCollector DB query failed", exc_info=True)
            return self._reading(0.0, "db_error")

        if not categories:
            return self._reading(0.0, "no_categories")

        worst_value = 0.0
        worst_source = "stable"

        for cat in categories:
            current_level = cat.get("current_level", 1)
            earned_level = cat.get("earned_level", 1)
            consecutive = cat.get("consecutive_corrections", 0)
            category_name = cat.get("category", "unknown")

            if current_level < earned_level:
                # Active regression — strongest signal, unless the recovery
                # path is already sitting with the user as a pending
                # earn-back proposal.
                if await self._earnback_pending(category_name):
                    if worst_value < 0.7:
                        worst_value = 0.7
                        worst_source = f"regression_pending_earnback_{category_name}"
                elif worst_value < 1.0:
                    worst_value = 1.0
                    worst_source = f"regression_{category_name}"
            elif consecutive >= self._REGRESSION_THRESHOLD - 1:
                # Near regression threshold
                if worst_value < 0.7:
                    worst_value = 0.7
                    worst_source = f"near_regression_{category_name}"
            elif consecutive > 0 and worst_value < 0.3:
                # Corrections accumulating
                worst_value = 0.3
                worst_source = f"corrections_{category_name}"

        return self._reading(worst_value, worst_source)

    async def _earnback_pending(self, category: str) -> bool:
        """True if a pending earn-back proposal exists for *category*."""
        try:
            cursor = await self._db.execute(
                "SELECT 1 FROM ego_proposals WHERE status = 'pending' "
                "AND action_type = 'autonomy_earnback' "
                "AND action_category = ? LIMIT 1",
                (category,),
            )
            return await cursor.fetchone() is not None
        except Exception:
            logger.debug("earnback-pending check failed", exc_info=True)
            return False

    def _reading(self, value: float, source: str) -> SignalReading:
        return SignalReading(
            name=self.signal_name,
            value=value,
            source=source,
            collected_at=datetime.now(UTC).isoformat(),
            baseline_note=(
                "0.0=stable autonomy (normal). 0.3=corrections accumulating, "
                "0.7=near regression OR regression with an earn-back proposal "
                "awaiting the user, 1.0=active regression with no pending "
                "earn-back path"
            ),
        )
