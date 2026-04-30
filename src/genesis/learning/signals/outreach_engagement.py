"""Real OutreachEngagementCollector — replaces stub in awareness/signals.py."""

from __future__ import annotations

from datetime import UTC, datetime

import aiosqlite

from genesis.awareness.types import SignalReading


class OutreachEngagementCollector:
    """Collects outreach engagement ratio from last 7 days."""

    signal_name = "outreach_engagement_data"

    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    async def collect(self) -> SignalReading:
        cursor = await self._db.execute(
            "SELECT engagement_outcome, COUNT(*) FROM outreach_history "
            "WHERE delivered_at >= datetime('now', '-7 days') "
            "AND engagement_outcome IS NOT NULL "
            "GROUP BY engagement_outcome"
        )
        rows = await cursor.fetchall()
        note = "Engagement ratio from last 7 days of outreach. 0.0=no outreach or no engagement"
        if not rows:
            return SignalReading(
                name=self.signal_name,
                value=0.0,
                source="outreach_history",
                collected_at=datetime.now(UTC).isoformat(),
                baseline_note=note,
            )
        total = sum(r[1] for r in rows)
        engaged = sum(r[1] for r in rows if r[0] == "engaged")
        value = engaged / total if total > 0 else 0.0
        return SignalReading(
            name=self.signal_name,
            value=value,
            source="outreach_history",
            collected_at=datetime.now(UTC).isoformat(),
            baseline_note=note,
        )
