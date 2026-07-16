"""Job health tracking for GenesisRuntime.

Owns the scheduled-job health state machine: recording success/failure,
loading persisted state at bootstrap, and writing updates back to the
``job_health`` table. Also houses ``register_channel`` for outreach
adapter registration because it shares the same "helper functions that
take the runtime as their first arg" shape.

Functions in this module take the runtime as an explicit first arg
rather than ``self``, so they can live here instead of as methods on
``GenesisRuntime``. ``GenesisRuntime`` keeps thin 2-line wrapper
methods on the class for the load/persist pair so test suites that
patch these names at class/instance level keep working without edits.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from genesis.runtime._core import GenesisRuntime

logger = logging.getLogger("genesis.runtime")

# WS-2 M9 — per-run job history debounce. A success earns a run_event only when
# >= this long since the job's last success; a failure earns one on streak onset
# and then at most once per interval during a sustained outage. Anchored on the
# already-persisted job_health columns (last_success / last_failure /
# consecutive_failures) so the debounce survives restart without new state.
_RUN_EVENT_MIN_INTERVAL = timedelta(hours=1)


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None


def _duration_ms(run_started_at: str | None, now_dt: datetime | None) -> int | None:
    """Real run duration, or None when no start marker exists (never guess).

    Deriving duration from ``last_run`` would yield the inter-run *cadence*, not
    the duration — a lying instrument. Only ``record_job_start`` sets a start
    marker, so duration is honest-or-NULL.
    """
    start_dt = _parse_iso(run_started_at)
    if start_dt is None or now_dt is None:
        return None
    delta_ms = (now_dt - start_dt).total_seconds() * 1000
    return int(delta_ms) if delta_ms >= 0 else None


def _append_job_run_event(
    rt: GenesisRuntime,
    job_name: str,
    *,
    status: str,
    run_started_at: str | None,
    duration_ms: int | None,
    error: str | None,
    now: str,
) -> None:
    """Fire-and-forget append to ``job_run_events`` (mirrors ``_persist_job_start``).

    Same loop-check-before-coroutine + ``tracked_task`` + swallow contract as the
    job_health persist path — a run-event write must NEVER throw into the ~62
    ``record_job_*`` callers, several on the 5-min awareness hot path.
    """
    if rt._db is None:
        return
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return

    from genesis.db.crud import job_run_events as _jre
    from genesis.util.tasks import tracked_task

    async def _write() -> None:
        try:
            await _jre.record_run_event(
                rt._db,
                job_name=job_name,
                status=status,
                run_started_at=run_started_at,
                duration_ms=duration_ms,
                error=error,
                recorded_at=now,
            )
        except sqlite3.Error:
            logger.error("DB error recording job run event for %s", job_name, exc_info=True)
        except Exception:
            logger.error("Failed to record job run event for %s", job_name, exc_info=True)

    try:
        tracked_task(_write(), name=f"job-run-event-{job_name}")
    except Exception:
        logger.error("Failed to schedule job run event for %s", job_name, exc_info=True)


async def load_persisted_job_health(rt: GenesisRuntime) -> None:
    """Load persisted job health rows from the DB into rt._job_health.

    Called once from ``bootstrap()`` after the DB is open. The
    ``job_health`` table may not exist on a fresh install — in that
    case we log at DEBUG and return cleanly; the first ``persist_job_health``
    call will create the table via the migration path.
    """
    if rt._db is None:
        return
    try:
        import aiosqlite

        async with rt._db.execute(
            "SELECT job_name, last_run, last_success, last_failure, "
            "last_error, consecutive_failures FROM job_health"
        ) as cur:
            for row in await cur.fetchall():
                rt._job_health[row[0]] = {
                    "last_run": row[1],
                    "last_success": row[2],
                    "last_failure": row[3],
                    "last_error": row[4],
                    "consecutive_failures": row[5],
                }
        if rt._job_health:
            logger.info("Loaded %d persisted job health entries", len(rt._job_health))
    except aiosqlite.OperationalError as exc:
        if "no such table" in str(exc):
            logger.debug("job_health table not yet available — will be created on first write")
        else:
            logger.error("Failed to load persisted job health", exc_info=True)
    except Exception:
        logger.error("Failed to load persisted job health", exc_info=True)


def clear_stale_job_failures(rt: GenesisRuntime) -> int:
    """Reset consecutive_failures for jobs that last failed before this startup.

    After a code fix + deploy, pre-restart failures are stale — the code
    that caused them no longer exists.  Resetting gives the job a clean
    slate to prove itself on the new code.

    Returns count of jobs cleared.
    """
    if not rt._job_health:
        return 0

    now_iso = datetime.now(UTC).isoformat()
    cleared = 0

    for job_name, entry in rt._job_health.items():
        if entry.get("consecutive_failures", 0) == 0:
            continue
        last_failure = entry.get("last_failure")
        if not last_failure:
            continue
        logger.info(
            "Cleared stale failures for job %s (last_failure=%s, was=%d)",
            job_name, last_failure, entry["consecutive_failures"],
        )
        entry["consecutive_failures"] = 0
        entry.pop("last_failure", None)
        entry.pop("last_error", None)
        persist_job_health(rt, job_name, entry, now_iso)
        cleared += 1

    return cleared


def persist_job_health(rt: GenesisRuntime, job_name: str, entry: dict, now: str) -> None:
    """Schedule a background write of the current job_health entry to the DB.

    Checks for a running event loop BEFORE creating the coroutine.
    Creating the coroutine eagerly and then catching ``RuntimeError``
    leaks an unawaited coroutine and emits ``RuntimeWarning`` every 60s.
    """
    if rt._db is None:
        return
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        logger.debug("No event loop — job health for %s persisted in-memory only", job_name)
        return

    from genesis.util.tasks import tracked_task

    snapshot = dict(entry)

    async def _write() -> None:
        try:
            await rt._db.execute(
                """INSERT INTO job_health
                   (job_name, last_run, last_success, last_failure, last_error,
                    consecutive_failures, total_runs, total_successes,
                    total_failures, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                   ON CONFLICT(job_name) DO UPDATE SET
                       last_run = excluded.last_run,
                       last_success = COALESCE(excluded.last_success, last_success),
                       -- NOT COALESCE: record_job_success / clear_stale_job_failures
                       -- carry NULL here to intentionally clear stale failure state on
                       -- recovery; the failure path always supplies both (WS-3b).
                       last_failure = excluded.last_failure,
                       last_error = excluded.last_error,
                       consecutive_failures = excluded.consecutive_failures,
                       total_runs = total_runs + 1,
                       total_successes = total_successes + CASE WHEN excluded.last_success IS NOT NULL THEN 1 ELSE 0 END,
                       total_failures = total_failures + CASE WHEN excluded.last_failure IS NOT NULL THEN 1 ELSE 0 END,
                       updated_at = excluded.updated_at
                """,
                (
                    job_name,
                    snapshot.get("last_run"),
                    snapshot.get("last_success"),
                    snapshot.get("last_failure"),
                    snapshot.get("last_error"),
                    snapshot.get("consecutive_failures", 0),
                    1 if snapshot.get("last_success") else 0,
                    1 if snapshot.get("last_failure") else 0,
                    now,
                ),
            )
            await rt._db.commit()
        except sqlite3.Error:
            logger.error("DB error persisting job health for %s", job_name, exc_info=True)
        except Exception:
            logger.error("Failed to persist job health for %s", job_name, exc_info=True)

    try:
        tracked_task(_write(), name=f"persist-job-health-{job_name}")
    except Exception:
        logger.error("Failed to schedule job health persistence for %s", job_name, exc_info=True)


def _persist_job_start(rt: GenesisRuntime, job_name: str, now: str) -> None:
    """Persist only ``last_run`` + ``updated_at`` — no counter increments.

    Unlike ``persist_job_health``, this does NOT increment ``total_runs``,
    ``total_successes``, or ``total_failures``.  Used exclusively by
    ``record_job_start`` so that a start + success/failure pair counts as
    one run, not two.
    """
    if rt._db is None:
        return
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return

    from genesis.util.tasks import tracked_task

    async def _write() -> None:
        try:
            await rt._db.execute(
                """INSERT INTO job_health
                   (job_name, last_run, consecutive_failures, total_runs,
                    total_successes, total_failures, updated_at)
                   VALUES (?, ?, 0, 0, 0, 0, ?)
                   ON CONFLICT(job_name) DO UPDATE SET
                       last_run = excluded.last_run,
                       updated_at = excluded.updated_at
                """,
                (job_name, now, now),
            )
            await rt._db.commit()
        except sqlite3.Error:
            logger.error("DB error persisting job start for %s", job_name, exc_info=True)
        except Exception:
            logger.error("Failed to persist job start for %s", job_name, exc_info=True)

    try:
        tracked_task(_write(), name=f"persist-job-start-{job_name}")
    except Exception:
        logger.error("Failed to schedule job start persistence for %s", job_name, exc_info=True)


def record_job_start(rt: GenesisRuntime, job_name: str) -> None:
    """Record that a scheduled job has started (in-memory + DB).

    Sets ``last_run`` immediately so crashes mid-execution are
    distinguishable from "never ran".  Does NOT touch
    ``consecutive_failures``, ``last_success``, or ``last_failure``.
    Uses a separate DB persist path that does NOT increment run counters.
    """
    now = datetime.now(UTC).isoformat()
    entry = rt._job_health.setdefault(job_name, {"consecutive_failures": 0})
    entry["last_run"] = now
    # WS-2 M9: in-memory start marker for honest duration_ms (consumed at
    # success/failure). Not persisted — a mid-run restart loses it → NULL duration.
    entry["run_started_at"] = now
    _persist_job_start(rt, job_name, now)


def record_job_success(rt: GenesisRuntime, job_name: str) -> None:
    """Record a successful scheduled job execution (in-memory + DB)."""
    now = datetime.now(UTC).isoformat()
    entry = rt._job_health.setdefault(job_name, {"consecutive_failures": 0})
    prev_success = entry.get("last_success")  # capture BEFORE the overwrite below
    run_started_at = entry.pop("run_started_at", None)  # consume start marker
    entry["last_run"] = now
    entry["last_success"] = now
    entry["consecutive_failures"] = 0
    # Clear stale failure data so _persist_job_health doesn't re-count old
    # failures in the SQL CASE WHEN excluded.last_failure IS NOT NULL check.
    entry.pop("last_failure", None)
    entry.pop("last_error", None)
    rt._persist_job_health(job_name, entry, now)

    # WS-2 M9: debounced success run-event — first success ever, or >= 1h since
    # the last one. Anchored on the just-captured prev_success (persisted +
    # reloaded at boot), so a sub-hourly poll writes nothing until an hour elapses.
    now_dt = _parse_iso(now)
    prev_dt = _parse_iso(prev_success)
    if prev_dt is None or now_dt is None or (now_dt - prev_dt) >= _RUN_EVENT_MIN_INTERVAL:
        _append_job_run_event(
            rt,
            job_name,
            status="success",
            run_started_at=run_started_at,
            duration_ms=_duration_ms(run_started_at, now_dt),
            error=None,
            now=now,
        )


def record_job_failure(rt: GenesisRuntime, job_name: str, error: str) -> None:
    """Record a failed scheduled job execution (in-memory + DB).

    When consecutive failures reach the retry threshold (3), triggers
    an automatic retry via the JobRetryRegistry if one is wired.
    """
    now = datetime.now(UTC).isoformat()
    entry = rt._job_health.setdefault(job_name, {"consecutive_failures": 0})
    prev_consecutive = entry.get("consecutive_failures", 0)  # 0 → this is a streak ONSET
    prev_failure = entry.get("last_failure")  # heartbeat anchor (captured pre-overwrite)
    run_started_at = entry.pop("run_started_at", None)  # consume start marker
    entry["last_run"] = now
    entry["last_failure"] = now
    entry["last_error"] = error
    entry["consecutive_failures"] = prev_consecutive + 1
    rt._persist_job_health(job_name, entry, now)

    # WS-2 M9: failure run-event on streak ONSET (prev consecutive == 0) OR an
    # hourly heartbeat during a sustained outage — bounds a stuck 60s poll to
    # ~24 rows/day while preserving every distinct failure episode. Independent
    # of and BEFORE the retry branch so neither can suppress the other.
    now_dt = _parse_iso(now)
    prev_fail_dt = _parse_iso(prev_failure)
    heartbeat_due = (
        prev_fail_dt is None or now_dt is None or (now_dt - prev_fail_dt) >= _RUN_EVENT_MIN_INTERVAL
    )
    if prev_consecutive == 0 or heartbeat_due:
        _append_job_run_event(
            rt,
            job_name,
            status="failed",
            run_started_at=run_started_at,
            duration_ms=_duration_ms(run_started_at, now_dt),
            error=error,
            now=now,
        )

    consecutive = entry["consecutive_failures"]
    if consecutive >= 3 and rt._job_retry_registry is not None:
        from genesis.util.tasks import tracked_task

        tracked_task(
            rt._job_retry_registry.attempt_retry(job_name),
            name=f"job_retry:{job_name}",
        )


def register_channel(
    rt: GenesisRuntime, name: str, adapter: object, *, recipient: str | None = None
) -> None:
    """Register a channel adapter for outreach delivery."""
    if rt._outreach_pipeline is not None:
        rt._outreach_pipeline._channels[name] = adapter
        if recipient:
            rt._outreach_pipeline._recipients[name] = recipient
    if (rt._outreach_scheduler is not None
            and not rt._outreach_scheduler.is_running):
        logger.info("First outreach channel '%s' registered — starting scheduler", name)
        rt._outreach_scheduler.start()
