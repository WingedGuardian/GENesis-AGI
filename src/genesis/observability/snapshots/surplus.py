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
    # success UNCONDITIONALLY every ~5 min at loop entry, so a stale pulse means the
    # dispatch loop stopped firing / a dispatch hung — a real fault the idle-proxy
    # `status` above hides (a wedged scheduler reads "idle" → green). `paused` is
    # excluded because a paused loop legitimately skips its success record
    # (scheduler.py:823 before :835).
    #
    # Robust-by-construction fail-loud: exactly ONE authoritative source (the
    # BOOTSTRAPPED runtime's in-memory surplus_dispatch pulse), and EVERY way the
    # read can be uncertain collapses to liveness_error (dashboard renders `unknown`),
    # never a defaulted-False that reads green. The uncertain cases, all closed here:
    #   - no runtime, or an unbootstrapped zombie singleton another MCP tool
    #     constructed (peek can return it; its pause state is meaningless) → its state
    #     would make liveness depend on MCP call order;
    #   - no pulse on record (never started / crashed before the first heartbeat,
    #     which scheduler.py:549-556 swallows) — NOT owned by the job_never_succeeded
    #     alarm (that needs last_run + >=3 failures);
    #   - a non-null but unparseable pulse (row corruption / legacy / manual data).
    # We read the IN-MEMORY pulse, not the DB copy: record_job_success updates
    # rt._job_health synchronously but persists fire-and-forget with swallowed DB
    # errors, so a DB read would go stale and FALSE-RED a healthy scheduler when
    # persistence lags. The in-memory value is the live pulse the loop actually writes.
    last_success_at = None
    stalled = False
    stall_reason = None
    liveness_error = False
    try:
        from genesis.observability.liveness import compute_pulse_liveness
        from genesis.runtime._core import GenesisRuntime

        rt = GenesisRuntime.peek()
        if rt is None or not rt.is_bootstrapped:
            raise RuntimeError("no bootstrapped runtime — cannot confirm liveness")
        raw_pulse = (
            (getattr(rt, "_job_health", None) or {}).get("surplus_dispatch") or {}
        ).get("last_success")
        interval = (
            getattr(surplus, "_dispatch_interval", 5) if surplus is not None else 5
        )
        live = compute_pulse_liveness(
            last_success_at=raw_pulse,
            expected_interval_minutes=interval,
            paused=bool(rt.paused),
        )
        # overdue_minutes is None iff the pulse is not a confirmable recent-past
        # timestamp — absent, unparseable, OR materially in the future (clock skew /
        # corruption). Either way liveness is not confirmable → unavailable, never green.
        if live.overdue_minutes is None:
            raise RuntimeError("no confirmable pulse on record — cannot confirm liveness")
        last_success_at = live.last_success_at
        stalled = live.stalled
        stall_reason = live.reason
    except Exception:
        logger.debug("surplus liveness unavailable", exc_info=True)
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
