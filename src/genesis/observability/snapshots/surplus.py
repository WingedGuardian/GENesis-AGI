"""Surplus scheduler snapshot."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import aiosqlite

    from genesis.surplus.scheduler import SurplusScheduler

logger = logging.getLogger(__name__)

# A status.json older than this is not trusted for liveness. The server rewrites
# the file every 60s; 300s is the watchdog's own staleness threshold for the same
# file (autonomy/watchdog.py, staleness_threshold_s).
STATUS_FILE_MAX_AGE_S = 300


@dataclass(frozen=True)
class StatusFilePulse:
    """The surplus pulse as the server wrote it into status.json.

    Values are passed through untyped from JSON; ``surplus_status`` validates
    them. ``written_at`` is the file's own top-level ``timestamp``.
    """

    last_success: object
    paused: object
    written_at: object


async def surplus_status(
    db: aiosqlite.Connection | None,
    surplus: SurplusScheduler | None,
    *,
    status_file_pulse: StatusFilePulse | None = None,
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
    # Robust-by-construction fail-loud: exactly ONE authoritative VALUE (the server
    # runtime's in-memory surplus_dispatch pulse), reachable two ways, and EVERY way
    # either read can be uncertain collapses to liveness_error (dashboard renders
    # `unknown`), never a defaulted-False that reads green.
    #   1. In-process (genesis-server): the BOOTSTRAPPED runtime's _job_health.
    #   2. Out-of-process (the standalone health MCP, which has no runtime): the
    #      same pulse and pause flag as the server wrote them into status.json
    #      (resilience/status_writer.py), passed in as ``status_file_pulse``.
    # The uncertain cases, all closed here:
    #   - path 1: no runtime, or an unbootstrapped zombie singleton another MCP tool
    #     constructed (peek can return it; its pause state is meaningless) → its state
    #     would make liveness depend on MCP call order;
    #   - path 2: a status.json older than STATUS_FILE_MAX_AGE_S, future-dated, or
    #     without a parseable timestamp. A frozen file is the dead-server case: its
    #     pulse ages too slowly to trip the 3h stall floor, and a pause flag frozen
    #     at true would suppress the stall forever. Also a pause flag that is not a
    #     real bool, or a pulse that is not a string (no coercion of JSON values);
    #   - no pulse on record (never started / crashed before the first heartbeat,
    #     which scheduler.py:549-556 swallows; or an older server that writes no
    #     liveness_pulses) — NOT owned by the job_never_succeeded alarm (that needs
    #     last_run + >=3 failures);
    #   - a non-null but unparseable pulse (row corruption / legacy / manual data).
    # We never read the DB copy of the pulse: record_job_success updates
    # rt._job_health synchronously but persists fire-and-forget with swallowed DB
    # errors, so a DB read would go stale and FALSE-RED a healthy scheduler when
    # persistence lags. The in-memory value is the live pulse the loop actually writes.
    last_success_at = None
    stalled = False
    stall_reason = None
    liveness_error = False
    liveness_source = "status_file" if status_file_pulse is not None else "runtime"
    try:
        from genesis.observability.liveness import (
            FUTURE_SKEW_TOLERANCE_MINUTES,
            compute_pulse_liveness,
            parse_iso_utc,
        )

        if status_file_pulse is not None:
            written = parse_iso_utc(status_file_pulse.written_at) if isinstance(
                status_file_pulse.written_at, str
            ) else None
            if written is None:
                raise RuntimeError("status file has no usable timestamp — cannot confirm liveness")
            age_s = (datetime.now(UTC) - written).total_seconds()
            if age_s > STATUS_FILE_MAX_AGE_S or age_s < -FUTURE_SKEW_TOLERANCE_MINUTES * 60:
                raise RuntimeError(f"status file is {age_s:.0f}s old — cannot confirm liveness")
            if not isinstance(status_file_pulse.paused, bool):
                raise RuntimeError("status file pause flag missing or not a bool")
            if not isinstance(status_file_pulse.last_success, str):
                raise RuntimeError("status file carries no surplus pulse")
            raw_pulse = status_file_pulse.last_success
            paused = status_file_pulse.paused
        else:
            from genesis.runtime._core import GenesisRuntime

            rt = GenesisRuntime.peek()
            if rt is None or not rt.is_bootstrapped:
                raise RuntimeError("no bootstrapped runtime — cannot confirm liveness")
            raw_pulse = (
                (getattr(rt, "_job_health", None) or {}).get("surplus_dispatch") or {}
            ).get("last_success")
            paused = bool(rt.paused)
        interval = (
            getattr(surplus, "_dispatch_interval", 5) if surplus is not None else 5
        )
        live = compute_pulse_liveness(
            last_success_at=raw_pulse,
            expected_interval_minutes=interval,
            paused=paused,
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
        "liveness_source": liveness_source,
    }
