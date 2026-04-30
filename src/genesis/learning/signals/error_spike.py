"""ErrorSpikeCollector — detects error spikes via observations table."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import aiosqlite

from genesis.awareness.types import SignalReading


class ErrorSpikeCollector:
    """Counts error-source observations in last 1h vs 24h baseline.

    Spike if hourly_count > 3 * (daily_count / 24).
    """

    signal_name = "software_error_spike"

    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    async def collect(self) -> SignalReading:
        now = datetime.now(UTC)
        one_hour_ago = (now - timedelta(hours=1)).isoformat()
        one_day_ago = (now - timedelta(hours=24)).isoformat()

        # Count errors in last hour
        cursor = await self._db.execute(
            "SELECT COUNT(*) FROM observations WHERE source = 'error' AND created_at >= ?",
            (one_hour_ago,),
        )
        hourly_count = (await cursor.fetchone())[0]

        # Count errors in last 24h
        cursor = await self._db.execute(
            "SELECT COUNT(*) FROM observations WHERE source = 'error' AND created_at >= ?",
            (one_day_ago,),
        )
        daily_count = (await cursor.fetchone())[0]

        if daily_count == 0:
            return self._reading(0.0, now)

        baseline = daily_count / 24.0
        threshold = 3.0 * baseline
        if threshold == 0:
            return self._reading(0.0, now)

        value = min(1.0, hourly_count / threshold)
        return self._reading(value, now)

    def _reading(self, value: float, now: datetime) -> SignalReading:
        return SignalReading(
            name=self.signal_name,
            value=value,
            source="observations",
            collected_at=now.isoformat(),
            baseline_note="0.0=no error spike (normal). Fires when hourly errors exceed 3x daily baseline",
        )
