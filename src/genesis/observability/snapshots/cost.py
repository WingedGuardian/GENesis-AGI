"""Cost tracking snapshot."""

from __future__ import annotations

import calendar
import logging
import sqlite3
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import aiosqlite

    from genesis.resilience.cc_budget import CCBudgetTracker
    from genesis.routing.cost_tracker import CostTracker

logger = logging.getLogger(__name__)


async def cost(
    db: aiosqlite.Connection | None,
    cost_tracker: CostTracker | None,
    cc_budget: CCBudgetTracker | None,
) -> dict:
    if not cost_tracker:
        return {"daily_usd": None, "monthly_usd": None, "budget_status": "unknown"}

    try:
        daily = await cost_tracker.get_period_cost("today")
        weekly = await cost_tracker.get_period_cost("this_week")
        monthly = await cost_tracker.get_period_cost("this_month")
        budget = await cost_tracker.check_budget()

        budget_monthly_limit = None
        budget_remaining = None
        budget_pct_used = None
        if db:
            try:
                cursor = await db.execute(
                    """SELECT limit_usd, warning_pct FROM budgets
                       WHERE budget_type = 'monthly' AND active = 1
                       ORDER BY created_at DESC LIMIT 1"""
                )
                row = await cursor.fetchone()
                if row:
                    budget_monthly_limit = row[0]
                    budget_remaining = round(row[0] - monthly, 4)
                    budget_pct_used = round(monthly / row[0] * 100, 1) if row[0] > 0 else 0
            except sqlite3.Error:
                logger.debug("Budget limit query failed", exc_info=True)

        forecast = None
        try:
            day_of_month = datetime.now(UTC).day
            if day_of_month > 0 and monthly > 0:
                now = datetime.now(UTC)
                days_in_month = calendar.monthrange(now.year, now.month)[1]
                forecast = round(monthly / day_of_month * days_in_month, 4)
        except ValueError:
            logger.debug("Forecast calculation failed", exc_info=True)

        # Per-provider cost breakdown (this month)
        cost_by_provider: list[dict] = []
        if db:
            try:
                cursor = await db.execute(
                    """SELECT provider,
                       COALESCE(SUM(CASE WHEN created_at >= date('now') THEN cost_usd ELSE 0 END), 0),
                       COALESCE(SUM(cost_usd), 0),
                       COUNT(*)
                       FROM cost_events
                       WHERE created_at >= date('now', 'start of month')
                       GROUP BY provider ORDER BY 3 DESC"""
                )
                for row in await cursor.fetchall():
                    cost_by_provider.append({
                        "provider": row[0],
                        "today_usd": round(row[1], 4),
                        "month_usd": round(row[2], 4),
                        "calls": row[3],
                    })
            except sqlite3.Error:
                logger.debug("Provider cost breakdown query failed", exc_info=True)

        # Per-call-site cost breakdown (this month)
        cost_by_call_site: list[dict] = []
        if db:
            try:
                cursor = await db.execute(
                    """SELECT json_extract(metadata, '$.call_site'),
                       COALESCE(SUM(cost_usd), 0),
                       COUNT(*)
                       FROM cost_events
                       WHERE created_at >= date('now', 'start of month')
                         AND metadata IS NOT NULL
                       GROUP BY 1 ORDER BY 2 DESC LIMIT 10"""
                )
                for row in await cursor.fetchall():
                    if row[0]:
                        cost_by_call_site.append({
                            "call_site": row[0],
                            "month_usd": round(row[1], 4),
                            "calls": row[2],
                        })
            except sqlite3.Error:
                logger.debug("Call-site cost breakdown query failed", exc_info=True)

        return {
            "daily_usd": round(daily, 4),
            "weekly_usd": round(weekly, 4),
            "monthly_usd": round(monthly, 4),
            "budget_status": str(budget).upper(),
            "budget_monthly_limit": budget_monthly_limit,
            "budget_remaining": budget_remaining,
            "budget_pct_used": budget_pct_used,
            "forecast_monthly_usd": forecast,
            "cost_by_provider": cost_by_provider,
            "cost_by_call_site": cost_by_call_site,
        }
    except Exception:
        return {"daily_usd": None, "monthly_usd": None, "budget_status": "error"}
