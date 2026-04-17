"""SurplusActivityCollector — reports surplus task health as awareness signal."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import aiosqlite

from genesis.awareness.types import SignalReading

logger = logging.getLogger(__name__)


class SurplusActivityCollector:
    """Reports surplus task health over the last 24 hours.

    | Condition                          | Signal Value |
    |-----------------------------------|-------------|
    | Idle or healthy (>80% success)    | 0.0         |
    | Concerning (20-50% failure rate)  | 0.5         |
    | Stuck tasks (running > 2h)        | 0.8         |
    | Broken (>50% failure rate)        | 1.0         |
    """

    signal_name = "surplus_activity"

    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    async def collect(self) -> SignalReading:
        now = datetime.now(UTC)
        cutoff = (now - timedelta(hours=24)).isoformat()

        try:
            # Count completed and failed tasks in last 24h
            cursor = await self._db.execute(
                "SELECT status, COUNT(*) FROM surplus_tasks "
                "WHERE created_at >= ? AND status IN ('completed', 'failed') "
                "GROUP BY status",
                (cutoff,),
            )
            counts = dict(await cursor.fetchall())
            completed = counts.get("completed", 0)
            failed = counts.get("failed", 0)

            # Count stuck tasks (running > 2h)
            stuck_cutoff = (now - timedelta(hours=2)).isoformat()
            cursor = await self._db.execute(
                "SELECT COUNT(*) FROM surplus_tasks "
                "WHERE status = 'running' AND started_at < ?",
                (stuck_cutoff,),
            )
            stuck = (await cursor.fetchone())[0]
        except Exception:
            logger.error("SurplusActivityCollector DB query failed", exc_info=True)
            return self._reading(0.0, "db_error")

        total = completed + failed
        if total == 0 and stuck == 0:
            return self._reading(0.0, "idle")

        # Stuck tasks are a stronger signal than failure rate
        if stuck > 0:
            return self._reading(0.8, f"stuck_{stuck}")

        failure_rate = failed / total if total > 0 else 0.0

        if failure_rate > 0.5:
            return self._reading(1.0, f"broken_{failed}/{total}")
        elif failure_rate > 0.2:
            return self._reading(0.5, f"concerning_{failed}/{total}")
        else:
            return self._reading(0.0, f"healthy_{failed}/{total}")

    def _reading(self, value: float, source: str) -> SignalReading:
        return SignalReading(
            name=self.signal_name,
            value=value,
            source=source,
            collected_at=datetime.now(UTC).isoformat(),
        )
