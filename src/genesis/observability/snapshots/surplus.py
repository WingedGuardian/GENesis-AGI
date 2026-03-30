"""Surplus scheduler snapshot."""

from __future__ import annotations

import contextlib
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import aiosqlite

    from genesis.surplus.scheduler import SurplusScheduler

logger = logging.getLogger(__name__)


async def surplus_status(
    db: aiosqlite.Connection | None,
    surplus: SurplusScheduler | None,
) -> dict:
    status = "unknown"
    queue_depth = 0
    tasks_completed_24h = 0
    tasks_failed_24h = 0

    if surplus:
        try:
            from genesis.runtime import GenesisRuntime
            rt = GenesisRuntime.instance()
            idle = rt.idle_detector or surplus._idle_detector
            is_idle = idle.is_idle()
            status = "idle" if is_idle else "dispatching"
        except Exception:
            status = "unknown"

        with contextlib.suppress(Exception):
            queue_depth = await surplus._queue.pending_count()

    if db:
        try:
            cursor = await db.execute(
                """SELECT status, COUNT(*) FROM surplus_tasks
                   WHERE created_at >= datetime('now', '-1 day')
                     AND status IN ('completed', 'failed', 'pending')
                   GROUP BY status"""
            )
            for row in await cursor.fetchall():
                if row[0] == "completed":
                    tasks_completed_24h = row[1]
                elif row[0] == "failed":
                    tasks_failed_24h = row[1]
                elif row[0] == "pending" and queue_depth == 0:
                    queue_depth = row[1]

            if status == "unknown":
                cursor = await db.execute(
                    """SELECT COUNT(*) FROM surplus_tasks
                       WHERE started_at >= datetime('now', '-10 minutes')
                         AND status IN ('running', 'completed', 'failed')"""
                )
                recent_row = await cursor.fetchone()
                if recent_row and recent_row[0] > 0:
                    status = "active"
                elif tasks_completed_24h > 0 or tasks_failed_24h > 0:
                    status = "idle"
        except Exception:
            pass

    return {
        "status": status,
        "queue_depth": queue_depth,
        "tasks_completed_24h": tasks_completed_24h,
        "tasks_failed_24h": tasks_failed_24h,
    }
