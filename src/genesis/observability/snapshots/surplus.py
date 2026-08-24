"""Surplus scheduler snapshot."""

from __future__ import annotations

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
            # Use peek() — never construct a zombie singleton from an
            # observability call (would mask real bootstrap failures elsewhere).
            from genesis.runtime._core import GenesisRuntime
            rt = GenesisRuntime.peek()
            idle = (rt.idle_detector if rt is not None else None) or surplus._idle_detector
            is_idle = idle.is_idle()
            status = "idle" if is_idle else "dispatching"
        except Exception:
            logger.warning("Failed to determine surplus status", exc_info=True)
            status = "unknown"

        try:
            queue_depth = await surplus._queue.pending_count()
        except Exception:
            logger.warning("Failed to query surplus queue depth", exc_info=True)

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

    # Truthful liveness (mirrors routes/ego.py:112-144). surplus_dispatch records
    # success UNCONDITIONALLY every ~5 min at loop entry, so a stale last_success
    # means the dispatch loop stopped firing / a dispatch hung — a real fault the
    # idle-proxy `status` above hides (a wedged scheduler reads "idle" → green).
    # Computed from job_health INDEPENDENT of the `surplus` handle so the standalone
    # MCP surface (surplus=None) is not a green hole. Fail-LOUD: any read error, or
    # an inability to read the pause state (no live runtime), → liveness_error
    # (dashboard renders `unknown`), NEVER a defaulted-False that reads green while
    # wedged. `paused` is excluded because a paused loop legitimately skips its
    # success record (scheduler.py:823 before :835).
    last_success_at = None
    stalled = False
    stall_reason = None
    liveness_error = False
    try:
        if db is None:
            raise RuntimeError("no db handle — cannot read job_health")
        from genesis.db.crud.job_health import get_job_last_success
        from genesis.observability.liveness import compute_pulse_liveness
        from genesis.runtime._core import GenesisRuntime

        rt = GenesisRuntime.peek()
        if rt is None:
            raise RuntimeError("no live runtime — cannot read pause state")
        interval = getattr(surplus, "_dispatch_interval", 5) if surplus is not None else 5
        live = compute_pulse_liveness(
            last_success_at=await get_job_last_success(db, "surplus_dispatch"),
            expected_interval_minutes=interval,
            paused=bool(rt.paused),
        )
        last_success_at = live.last_success_at
        stalled = live.stalled
        stall_reason = live.reason
    except Exception:
        logger.debug("surplus liveness computation failed", exc_info=True)
        liveness_error = True

    return {
        "status": status,
        "queue_depth": queue_depth,
        "tasks_completed_24h": tasks_completed_24h,
        "tasks_failed_24h": tasks_failed_24h,
        "last_success_at": last_success_at,
        "stalled": stalled,
        "stall_reason": stall_reason,
        "liveness_error": liveness_error,
    }
