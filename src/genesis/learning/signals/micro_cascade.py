"""MicroCascadeCollector — counts Micro ticks since last Light reflection."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import aiosqlite

from genesis.awareness.types import SignalReading
from genesis.db.crud import awareness_ticks

logger = logging.getLogger(__name__)


class MicroCascadeCollector:
    """Counts Micro-depth ticks since last Light-depth tick.

    Implements the Micro -> Light escalation from the design doc.
    Normalizes: 0 micros = 0.0, 5+ micros = 1.0.
    """

    signal_name = "micro_count_since_light"

    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    async def collect(self) -> SignalReading:
        last_light = await awareness_ticks.last_at_depth(self._db, "Light")

        if last_light:
            cutoff = last_light["created_at"]
        else:
            # No Light tick ever — count recent Micros (last 24h)
            cutoff_dt = datetime.now(UTC) - timedelta(hours=24)
            cutoff = cutoff_dt.isoformat()

        # Count Micro ticks since cutoff
        try:
            cursor = await self._db.execute(
                """SELECT COUNT(*) FROM awareness_ticks
                   WHERE classified_depth = 'Micro'
                   AND created_at > ?""",
                (cutoff,),
            )
            row = await cursor.fetchone()
            count = row[0] if row else 0
        except Exception:
            logger.error("MicroCascadeCollector DB query failed", exc_info=True)
            count = 0

        value = min(1.0, count / 5.0)

        return SignalReading(
            name=self.signal_name,
            value=round(value, 3),
            source="awareness_ticks",
            collected_at=datetime.now(UTC).isoformat(),
            baseline_note="Micro ticks since last Light. 0.0=Light just ran, 1.0=5+ micro ticks without light",
        )
