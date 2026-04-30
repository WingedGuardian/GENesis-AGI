"""LightCascadeCollector — counts Light ticks since last Deep reflection."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import aiosqlite

from genesis.awareness.types import SignalReading
from genesis.db.crud import awareness_ticks

logger = logging.getLogger(__name__)


class LightCascadeCollector:
    """Counts Light-depth ticks since last Deep-depth tick.

    Implements the Light -> Deep escalation bridge.
    Normalizes: 0 lights = 0.0, 3+ lights = 1.0.

    Threshold is 3 (vs micro_cascade's 5) because Light ticks are rarer
    and each carries more synthesized content.
    """

    signal_name = "light_count_since_deep"

    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    async def collect(self) -> SignalReading:
        last_deep = await awareness_ticks.last_at_depth(self._db, "Deep")

        if last_deep:
            cutoff = last_deep["created_at"]
        else:
            # No Deep tick ever — count recent Lights (last 7 days)
            cutoff_dt = datetime.now(UTC) - timedelta(days=7)
            cutoff = cutoff_dt.isoformat()

        # Count Light ticks since cutoff
        try:
            cursor = await self._db.execute(
                """SELECT COUNT(*) FROM awareness_ticks
                   WHERE classified_depth = 'Light'
                   AND created_at > ?""",
                (cutoff,),
            )
            row = await cursor.fetchone()
            count = row[0] if row else 0
        except Exception:
            logger.error("LightCascadeCollector DB query failed", exc_info=True)
            count = 0

        value = min(1.0, count / 3.0)

        return SignalReading(
            name=self.signal_name,
            value=round(value, 3),
            source="awareness_ticks",
            collected_at=datetime.now(UTC).isoformat(),
            baseline_note="Light ticks since last Deep. 0.0=Deep just ran, 1.0=3+ light ticks without deep",
        )
