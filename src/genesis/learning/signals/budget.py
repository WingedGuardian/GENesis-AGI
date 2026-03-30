"""BudgetCollector — signal for daily budget consumption percentage."""

from __future__ import annotations

from datetime import UTC, datetime

import aiosqlite

from genesis.awareness.types import SignalReading

DEFAULT_DAILY_BUDGET_USD = 5.0


class BudgetCollector:
    """Queries cost_events for today's spend, normalizes against daily budget."""

    signal_name = "budget_pct_consumed"

    def __init__(self, db: aiosqlite.Connection, *, daily_budget: float = DEFAULT_DAILY_BUDGET_USD) -> None:
        self._db = db
        self._daily_budget = daily_budget

    async def collect(self) -> SignalReading:
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        cursor = await self._db.execute(
            "SELECT COALESCE(SUM(cost_usd), 0.0) FROM cost_events WHERE created_at >= ?",
            (today,),
        )
        row = await cursor.fetchone()
        total = row[0] if row else 0.0
        value = min(1.0, total / self._daily_budget) if self._daily_budget > 0 else 0.0
        return SignalReading(
            name=self.signal_name,
            value=value,
            source="cost_events",
            collected_at=datetime.now(UTC).isoformat(),
        )
