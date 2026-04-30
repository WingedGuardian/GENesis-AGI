"""MemoryBacklogCollector — counts unprocessed observations as memory backlog."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import aiosqlite

from genesis.awareness.types import SignalReading

BACKLOG_CEILING = 100


class MemoryBacklogCollector:
    """Counts observations with retrieved_count=0 from last 7 days."""

    signal_name = "unprocessed_memory_backlog"

    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    async def collect(self) -> SignalReading:
        cutoff = (datetime.now(UTC) - timedelta(days=7)).isoformat()

        cursor = await self._db.execute(
            "SELECT COUNT(*) FROM observations WHERE retrieved_count = 0 AND created_at >= ?",
            (cutoff,),
        )
        count = (await cursor.fetchone())[0]
        value = min(1.0, count / BACKLOG_CEILING)
        return SignalReading(
            name=self.signal_name,
            value=value,
            source="observations",
            collected_at=datetime.now(UTC).isoformat(),
            baseline_note="Unprocessed observations from last 7 days. Low values normal; rises if retrieval pipeline stalls",
        )
