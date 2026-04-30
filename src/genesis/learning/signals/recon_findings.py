"""ReconFindingsCollector — counts unresolved recon findings."""

from __future__ import annotations

from datetime import UTC, datetime

import aiosqlite

from genesis.awareness.types import SignalReading

FINDINGS_CEILING = 10


class ReconFindingsCollector:
    """Counts unresolved recon findings (observations with source='recon', type='finding')."""

    signal_name = "recon_findings_pending"

    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    async def collect(self) -> SignalReading:
        cursor = await self._db.execute(
            "SELECT COUNT(*) FROM observations "
            "WHERE source = 'recon' AND type = 'finding' AND resolved = 0"
        )
        count = (await cursor.fetchone())[0]
        value = min(1.0, count / FINDINGS_CEILING)
        return SignalReading(
            name=self.signal_name,
            value=value,
            source="observations",
            collected_at=datetime.now(UTC).isoformat(),
            baseline_note="Unresolved recon findings. 0.0=none pending (normal). Normalized against ceiling of 10",
        )
