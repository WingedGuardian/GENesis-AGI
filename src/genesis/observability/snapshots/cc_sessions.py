"""CC sessions snapshot — foreground/background session counts and budget."""

from __future__ import annotations

import logging
import sqlite3
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import aiosqlite

    from genesis.resilience.cc_budget import CCBudgetTracker
    from genesis.resilience.state import ResilienceStateMachine

logger = logging.getLogger(__name__)


async def cc_sessions(
    db: aiosqlite.Connection | None,
    cc_budget: CCBudgetTracker | None,
    state_machine: ResilienceStateMachine | None,
) -> dict:
    if not db:
        return {"foreground": {"status": "unknown"}, "background": {"status": "unknown"}}

    try:
        cursor = await db.execute(
            """SELECT session_type, COUNT(*) FROM cc_sessions
               WHERE status = 'active' GROUP BY session_type""",
        )
        rows = await cursor.fetchall()
        counts = {row[0]: row[1] for row in rows}
    except sqlite3.Error:
        logger.debug("CC session count query failed", exc_info=True)
        counts = {}

    fg_active = counts.get("foreground", 0)
    bg_active = counts.get("background", 0) + counts.get("reflection", 0)

    budget_str = "unknown"
    cc_status = "unknown"
    if cc_budget:
        try:
            usage = await cc_budget.get_usage_pct()
            status = await cc_budget.get_status()
            budget_str = f"{int(usage * cc_budget._max)}/{cc_budget._max}"
            cc_status = "healthy" if status.name == "NORMAL" else status.name.lower()
        except Exception:
            cc_status = "error"

    avg_duration_ms = {}
    failed_24h = 0
    try:
        cursor = await db.execute(
            """SELECT session_type, AVG(duration_ms) as avg_ms
               FROM cc_sessions
               WHERE status = 'completed' AND ended_at >= datetime('now', '-1 day')
               GROUP BY session_type"""
        )
        for row in await cursor.fetchall():
            avg_duration_ms[row["session_type"]] = round(row["avg_ms"])

        cursor = await db.execute(
            """SELECT COUNT(*) as cnt FROM cc_sessions
               WHERE status = 'failed' AND ended_at >= datetime('now', '-1 day')"""
        )
        row = await cursor.fetchone()
        failed_24h = row["cnt"] if row else 0
    except (sqlite3.Error, TypeError, KeyError):
        logger.debug("CC session stats query failed", exc_info=True)

    hourly_burn_rate = 0.0
    max_per_hour = 20
    try:
        cursor = await db.execute(
            """SELECT strftime('%Y-%m-%d %H:00:00', started_at) as hour, COUNT(*) as cnt
               FROM cc_sessions
               WHERE started_at >= datetime('now', '-24 hours')
               GROUP BY hour ORDER BY hour"""
        )
        hourly_data = [(row[0], row[1]) for row in await cursor.fetchall()]
        if hourly_data:
            hourly_burn_rate = round(
                sum(h[1] for h in hourly_data) / len(hourly_data), 1
            )
        if cc_budget:
            max_per_hour = cc_budget._max
    except (sqlite3.Error, TypeError, IndexError):
        logger.debug("Hourly burn rate query failed", exc_info=True)

    shadow_cost_today = 0.0
    shadow_cost_month = 0.0
    total_tokens_today = 0
    rate_limited_24h = 0
    try:
        cursor = await db.execute(
            """SELECT COALESCE(SUM(cost_usd), 0),
                      COALESCE(SUM(COALESCE(input_tokens,0) + COALESCE(output_tokens,0)), 0)
               FROM cc_sessions WHERE started_at >= date('now')"""
        )
        row = await cursor.fetchone()
        if row:
            shadow_cost_today = round(row[0], 4)
            total_tokens_today = row[1]

        cursor = await db.execute(
            """SELECT COALESCE(SUM(cost_usd), 0)
               FROM cc_sessions WHERE started_at >= date('now', 'start of month')"""
        )
        row = await cursor.fetchone()
        shadow_cost_month = round(row[0], 4) if row else 0.0

        cursor = await db.execute(
            """SELECT COUNT(*) FROM cc_sessions
               WHERE rate_limited_at IS NOT NULL
                 AND rate_limited_at >= datetime('now', '-1 day')"""
        )
        row = await cursor.fetchone()
        rate_limited_24h = row[0] if row else 0
    except (sqlite3.Error, TypeError, IndexError):
        logger.debug("Shadow cost query failed", exc_info=True)

    cc_realtime_status = "unknown"
    fg_status = "healthy"  # CC is always "on" — default to healthy
    if state_machine:
        try:
            from genesis.resilience.state import CCStatus

            cc_state = state_machine.current.cc
            cc_realtime_status = cc_state.name

            # Reconcile stale state machine with budget tracker (source of truth).
            # The state machine latches RATE_LIMITED from CCInvoker errors but
            # never clears when foreground sessions work fine or background
            # sessions recover via a different code path.
            if cc_budget and cc_state in (CCStatus.RATE_LIMITED, CCStatus.THROTTLED):
                try:
                    actual = await cc_budget.get_status()
                    if actual == CCStatus.NORMAL:
                        transitions = state_machine.update_cc(CCStatus.NORMAL)
                        # Only update display if transition was actually applied
                        # (flapping protection may suppress it)
                        if transitions and not transitions[0].suppressed:
                            cc_state = CCStatus.NORMAL
                            cc_realtime_status = "NORMAL"
                            logger.info("CC status auto-recovered: budget tracker says NORMAL")
                except Exception:
                    logger.debug("Budget tracker reconciliation failed", exc_info=True)

            fg_status = {
                CCStatus.NORMAL: "healthy",
                CCStatus.THROTTLED: "degraded",
                CCStatus.RATE_LIMITED: "degraded",
                CCStatus.UNAVAILABLE: "down",
            }.get(cc_state, "healthy")
        except (AttributeError, ImportError):
            pass  # keep defaults

    return {
        "foreground": {"status": fg_status, "active": fg_active},
        "background": {
            "status": cc_status,
            "active": bg_active,
            "hourly_budget": budget_str,
        },
        "avg_duration_ms_24h": avg_duration_ms,
        "failed_24h": failed_24h,
        "hourly_burn_rate": hourly_burn_rate,
        "max_per_hour": max_per_hour,
        "shadow_cost_today": shadow_cost_today,
        "shadow_cost_month": shadow_cost_month,
        "total_tokens_today": total_tokens_today,
        "rate_limited_24h": rate_limited_24h,
        "realtime_status": cc_realtime_status,
    }
