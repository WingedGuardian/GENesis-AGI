"""Cost tracking and budget enforcement for compute routing."""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime, timedelta

import aiosqlite

from genesis.db.crud import budgets as budgets_crud
from genesis.db.crud import cost_events as cost_events_crud
from genesis.observability.events import GenesisEventBus
from genesis.observability.types import Severity, Subsystem
from genesis.routing.types import BudgetStatus, CallResult

# Map budget_type to period name used in _period_start
_BUDGET_PERIOD = {
    "daily": "today",
    "weekly": "this_week",
    "monthly": "this_month",
}


class CostTracker:
    _EVENT_THROTTLE_S = 300.0  # 5 min between identical budget events

    def __init__(self, db: aiosqlite.Connection, *, clock=None, event_bus: GenesisEventBus | None = None):
        self.db = db
        self._clock = clock or (lambda: datetime.now(UTC))
        self._event_bus = event_bus
        self._last_event_at: dict[str, float] = {}  # "daily_exceeded" -> monotonic time

    async def record(
        self, call_site_id: str, provider: str, result: CallResult,
        *, cost_known: bool = True,
    ) -> None:
        """Record an LLM call as a cost event."""
        await cost_events_crud.create(
            self.db,
            id=str(uuid.uuid4()),
            event_type="llm_call",
            model=provider,
            provider=provider,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            cost_usd=result.cost_usd,
            cost_known=cost_known,
            metadata={"call_site": call_site_id},
            created_at=self._clock().isoformat(),
        )

    async def check_budget(self, *, task_id: str | None = None) -> BudgetStatus:
        """Check all budget periods and return the worst status."""
        worst = BudgetStatus.UNDER_LIMIT
        for budget_type in ("daily", "weekly", "monthly"):
            status = await self._check_period(budget_type)
            if status == BudgetStatus.EXCEEDED:
                return BudgetStatus.EXCEEDED
            if status == BudgetStatus.WARNING:
                worst = BudgetStatus.WARNING
        return worst

    async def get_period_cost(self, period: str) -> float:
        """Get total cost for a period: 'today', 'this_week', 'this_month'."""
        since = self._period_start(period)
        return await cost_events_crud.sum_cost(self.db, since=since)

    async def _check_period(self, budget_type: str) -> BudgetStatus:
        """Check a single budget period and return its status."""
        budgets = await budgets_crud.list_active(self.db, budget_type=budget_type)
        if not budgets:
            return BudgetStatus.UNDER_LIMIT
        budget = budgets[0]
        period = _BUDGET_PERIOD[budget_type]
        since = self._period_start(period)
        total = await cost_events_crud.sum_cost(self.db, since=since)
        limit_usd = budget["limit_usd"]
        warning_pct = budget["warning_pct"]
        if total >= limit_usd:
            event_key = f"{budget_type}_exceeded"
            now_mono = time.monotonic()
            if now_mono - self._last_event_at.get(event_key, 0) >= self._EVENT_THROTTLE_S:
                if self._event_bus:
                    await self._event_bus.emit(
                        Subsystem.ROUTING, Severity.ERROR,
                        "budget.exceeded",
                        f"{budget_type} budget exceeded: ${total:.4f} >= ${limit_usd:.4f}",
                        budget_type=budget_type, total=total, limit=limit_usd,
                    )
                self._last_event_at[event_key] = now_mono
            return BudgetStatus.EXCEEDED
        if total >= limit_usd * warning_pct:
            event_key = f"{budget_type}_warning"
            now_mono = time.monotonic()
            if now_mono - self._last_event_at.get(event_key, 0) >= self._EVENT_THROTTLE_S:
                if self._event_bus:
                    await self._event_bus.emit(
                        Subsystem.ROUTING, Severity.WARNING,
                        "budget.warning",
                        f"{budget_type} budget warning: ${total:.4f} / ${limit_usd:.4f}",
                        budget_type=budget_type, total=total, limit=limit_usd,
                    )
                self._last_event_at[event_key] = now_mono
            return BudgetStatus.WARNING
        return BudgetStatus.UNDER_LIMIT

    def _period_start(self, period: str) -> str:
        """Return ISO timestamp for start of the given period."""
        now = self._clock()
        if period == "today":
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        elif period == "this_week":
            # Monday midnight
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            start -= timedelta(days=start.weekday())
        elif period == "this_month":
            start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        else:
            msg = f"Unknown period: {period}"
            raise ValueError(msg)
        return start.isoformat()
