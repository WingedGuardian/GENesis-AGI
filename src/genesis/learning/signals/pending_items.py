"""PendingItemCollector — signal for stale pending items in cognitive state."""

from __future__ import annotations

import re
from datetime import UTC, datetime

import aiosqlite

from genesis.awareness.types import SignalReading

STALE_THRESHOLD_DAYS = 3


class PendingItemCollector:
    """Reads cognitive state pending actions, returns stale-item urgency signal.

    Signal value: 1.0 = missing/error state (triggers deep reflection to generate it),
    0.0 = no stale items, up to 1.0 = items pending > 7 days.
    Intermediate: linear interpolation between 3-day and 7-day thresholds.
    """

    signal_name = "stale_pending_items"

    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    async def collect(self) -> SignalReading:
        now = datetime.now(UTC)
        try:
            cursor = await self._db.execute(
                "SELECT content, created_at FROM cognitive_state "
                "WHERE section = 'active_context' ORDER BY created_at DESC LIMIT 1"
            )
            row = await cursor.fetchone()
        except Exception:
            return self._reading(1.0, "db_error")

        if not row:
            return self._reading(1.0, "no_cognitive_state")

        content, created_at_str = row
        try:
            created_at = datetime.fromisoformat(created_at_str)
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=UTC)
        except (ValueError, TypeError):
            return self._reading(1.0, "bad_timestamp")

        age = now - created_at
        age_days = age.total_seconds() / 86400

        # Count pending items
        pending_count = 0
        in_pending = False
        for line in content.split("\n"):
            if "**Pending Actions**" in line:
                in_pending = True
                continue
            if in_pending:
                if line.strip().startswith("**") and "Pending" not in line:
                    break
                if line.strip() and re.match(r"\d+\.", line.strip()):
                    pending_count += 1

        if pending_count == 0:
            return self._reading(0.0, "no_pending_items")

        if age_days < STALE_THRESHOLD_DAYS:
            return self._reading(0.0, f"{pending_count}_items_{age_days:.1f}_days")

        # Linear interpolation: 3 days = 0.3, 7 days = 1.0
        value = min(1.0, 0.3 + (age_days - STALE_THRESHOLD_DAYS) * 0.7 / 4)
        return self._reading(
            value,
            f"{pending_count}_items_{age_days:.1f}_days_STALE",
        )

    def _reading(self, value: float, source: str) -> SignalReading:
        return SignalReading(
            name=self.signal_name,
            value=value,
            source=source,
            collected_at=datetime.now(UTC).isoformat(),
            baseline_note="0.0=no stale pending items. Rises when follow-ups/tasks age past 3 days without resolution",
        )
