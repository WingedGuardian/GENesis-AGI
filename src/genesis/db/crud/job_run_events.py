"""CRUD for ``job_run_events`` — per-run scheduled-job history (WS-2 M9).

Append-only sensor table. The debounce decision (which runs earn a row) lives
at the write source in :mod:`genesis.runtime._job_health`; this module is the
mechanical insert + the read/prune helpers. The ledger's ``scheduled_job``
grader (WS-2 P2) reads ``list_runs_for_day`` to score ``runs_clean_day`` /
``runtime_ms_le``.
"""

from __future__ import annotations

import uuid

import aiosqlite


async def record_run_event(
    db: aiosqlite.Connection,
    *,
    job_name: str,
    status: str,
    run_started_at: str | None = None,
    duration_ms: int | None = None,
    error: str | None = None,
    recorded_at: str,
) -> None:
    """Append one run event. ``status`` is 'success' or 'failed'.

    ``recorded_at`` is supplied by the caller (the runtime already stamps a
    single ``now`` per record_job_* call) so the event time matches the
    ``job_health`` row written in the same tick.
    """
    await db.execute(
        """INSERT INTO job_run_events
           (id, job_name, status, run_started_at, duration_ms, error, recorded_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            uuid.uuid4().hex,
            job_name,
            status,
            run_started_at,
            duration_ms,
            error,
            recorded_at,
        ),
    )
    await db.commit()


async def list_runs_for_day(db: aiosqlite.Connection, job_name: str, day: str) -> list[dict]:
    """All run events for ``job_name`` on ``day`` (YYYY-MM-DD, matched on recorded_at)."""
    cursor = await db.execute(
        "SELECT id, job_name, status, run_started_at, duration_ms, error, recorded_at "
        "FROM job_run_events WHERE job_name = ? AND substr(recorded_at, 1, 10) = ? "
        "ORDER BY recorded_at",
        (job_name, day),
    )
    return [dict(r) for r in await cursor.fetchall()]


async def count_recent_by_status(db: aiosqlite.Connection, *, since: str) -> dict[str, int]:
    """{status: count} for events recorded at/after ``since`` (ISO). Ops/coverage view."""
    cursor = await db.execute(
        "SELECT status, COUNT(*) FROM job_run_events WHERE recorded_at >= ? GROUP BY status",
        (since,),
    )
    return {row[0]: row[1] for row in await cursor.fetchall()}


async def prune_older_than(db: aiosqlite.Connection, *, days: int = 90) -> int:
    """Delete run events older than ``days``. Returns rows deleted.

    Signature matches the drip-retention prune contract
    (``prune_older_than(db, *, days=...)``) so it registers cleanly in
    ``_wire_drip_retention_jobs``.
    """
    cursor = await db.execute(
        "DELETE FROM job_run_events WHERE recorded_at < datetime('now', ?)",
        (f"-{int(days)} days",),
    )
    await db.commit()
    return cursor.rowcount if cursor.rowcount is not None else 0
