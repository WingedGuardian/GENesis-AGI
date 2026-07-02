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
