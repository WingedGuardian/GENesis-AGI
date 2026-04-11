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
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from genesis.runtime._core import GenesisRuntime

logger = logging.getLogger("genesis.runtime")


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
                       last_failure = COALESCE(excluded.last_failure, last_failure),
                       last_error = COALESCE(excluded.last_error, last_error),
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


def record_job_success(rt: GenesisRuntime, job_name: str) -> None:
    """Record a successful scheduled job execution (in-memory + DB)."""
    now = datetime.now(UTC).isoformat()
    entry = rt._job_health.setdefault(job_name, {"consecutive_failures": 0})
    entry["last_run"] = now
    entry["last_success"] = now
    entry["consecutive_failures"] = 0
    # Clear stale failure data so _persist_job_health doesn't re-count old
    # failures in the SQL CASE WHEN excluded.last_failure IS NOT NULL check.
    entry.pop("last_failure", None)
    entry.pop("last_error", None)
    rt._persist_job_health(job_name, entry, now)


def record_job_failure(rt: GenesisRuntime, job_name: str, error: str) -> None:
    """Record a failed scheduled job execution (in-memory + DB).

    When consecutive failures reach the retry threshold (3), triggers
    an automatic retry via the JobRetryRegistry if one is wired.
    """
    now = datetime.now(UTC).isoformat()
    entry = rt._job_health.setdefault(job_name, {"consecutive_failures": 0})
    entry["last_run"] = now
    entry["last_failure"] = now
    entry["last_error"] = error
    entry["consecutive_failures"] = entry.get("consecutive_failures", 0) + 1
    rt._persist_job_health(job_name, entry, now)

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
