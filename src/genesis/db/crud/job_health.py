"""Read-side queries for the ``job_health`` table.

Writes live in :mod:`genesis.runtime._job_health` (record_job_success /
_failure / clear_stale_job_failures). This module holds the observability
READ queries consumed by the health MCP + dashboard.
"""

from __future__ import annotations

import aiosqlite


async def get_stale_jobs(
    db: aiosqlite.Connection, *, threshold_days: float
) -> list[dict]:
    """Jobs that have RUN more than ``threshold_days`` since they last SUCCEEDED.

    Returns ``{job_name, last_success, gap_days}`` rows, widest gap first. The
    ``last_run − last_success`` gap survives the per-restart ``consecutive_failures``
    reset (``clear_stale_job_failures`` never touches ``last_run``/``last_success``),
    so it is the honest "running but not succeeding" signal — one a healthy job
    reads as 0 (a successful run writes both columns together).
    """
    cursor = await db.execute(
        "SELECT job_name, last_success, "
        "julianday(last_run) - julianday(last_success) AS gap_days "
        "FROM job_health "
        "WHERE last_run IS NOT NULL AND last_success IS NOT NULL "
        "AND julianday(last_run) - julianday(last_success) > ? "
        "ORDER BY gap_days DESC",
        (threshold_days,),
    )
    return [dict(r) for r in await cursor.fetchall()]


async def get_never_succeeded_jobs(
    db: aiosqlite.Connection, *, min_failures: int = 3, recent_since: str | None = None
) -> list[dict]:
    """Jobs that have RUN and FAILED repeatedly but NEVER once succeeded.

    :func:`get_stale_jobs` is structurally BLIND to these: it filters
    ``last_success IS NOT NULL``, and a never-succeeded job has ``last_success``
    NULL forever (so its ``last_run − last_success`` gap is undefined). The
    ``consecutive_failures`` counter also misses them (``clear_stale_job_failures``
    resets it to 0 on every restart). The lifetime ``total_successes`` /
    ``total_failures`` counters are MONOTONIC and survive that reset, so
    ``total_successes = 0 AND total_failures >= min_failures`` is the honest,
    restart-proof "has been failing since it first ran, never succeeded" signal.
    ``min_failures`` (default 3) avoids alarming a brand-new job that has failed
    only once or twice transiently.

    ``recent_since`` (an ISO timestamp) bounds the alarm to jobs still running
    recently: nothing auto-purges ``job_health`` rows, so a job that failed, never
    succeeded, then was disabled/removed would otherwise fire this WARNING forever.
    When set, only rows with ``last_run >= recent_since`` are returned. ``julianday``
    parses both ``+00:00`` and ``Z`` suffixes, so the compare is tz-suffix-safe (a
    lexical string compare is not). Left ``None`` (no bound) for raw-query callers/tests.
    The ``mcp/health/errors.py`` caller passes ~35d, covering the slowest scheduled
    cadence (weekly) with wide margin plus a plausible monthly actuator; a job that runs
    LESS often than the caller's window falls through BOTH this alarm and
    ``get_stale_jobs`` (which needs a prior success) — an accepted limitation to revisit
    if a monthly-or-slower actuator is ever added.

    CONTRACT: this assumes a healthy job records ``record_job_success`` under the SAME
    job_name on its happy path (bumping ``total_successes``). A job that only ever
    records failures (never success) would trip it while running — that job's
    instrumentation should be fixed, not the alarm. Returns
    ``{job_name, total_failures, last_error}`` rows, most failures first. (Origin: an
    8-day OAuth outage on a daily actuator that had never succeeded stayed invisible on
    every alarm surface, 2026-08.)
    """
    query = (
        "SELECT job_name, total_failures, last_error FROM job_health "
        "WHERE last_run IS NOT NULL AND total_successes = 0 AND total_failures >= ?"
    )
    params: list = [min_failures]
    if recent_since is not None:
        query += " AND julianday(last_run) >= julianday(?)"
        params.append(recent_since)
    query += " ORDER BY total_failures DESC"
    cursor = await db.execute(query, params)
    return [dict(r) for r in await cursor.fetchall()]


async def get_job_last_success(
    db: aiosqlite.Connection, job_name: str
) -> str | None:
    """Return the ISO ``last_success`` timestamp for ``job_name`` (or None).

    Used by the ego cadence to anchor its restart-safe boot first-fire to the
    last time this ego actually cycled (see
    ``EgoCadenceManager._compute_boot_first_fire``).
    """
    cursor = await db.execute(
        "SELECT last_success FROM job_health WHERE job_name = ?",
        (job_name,),
    )
    row = await cursor.fetchone()
    return row[0] if row else None
