"""CRUD operations for user_jobs and user_job_runs tables."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import aiosqlite


async def create_job(
    db: aiosqlite.Connection,
    *,
    title: str,
    cron_expression: str,
    dispatch_prompt: str,
    job_type: str = "generic",
    config_json: dict | None = None,
    description: str | None = None,
    profile: str = "observe",
    model: str = "sonnet",
    effort: str = "medium",
) -> str:
    """Insert a new user job. Returns the generated job ID."""
    job_id = str(uuid.uuid4())
    await db.execute(
        """INSERT INTO user_jobs
           (id, title, description, cron_expression, job_type, config_json,
            dispatch_prompt, profile, model, effort)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            job_id, title, description, cron_expression, job_type,
            json.dumps(config_json) if config_json else None,
            dispatch_prompt, profile, model, effort,
        ),
    )
    await db.commit()
    return job_id


async def get_job(db: aiosqlite.Connection, job_id: str) -> dict | None:
    """Return a single job by ID, or None."""
    cursor = await db.execute(
        "SELECT * FROM user_jobs WHERE id = ?", (job_id,),
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def list_jobs(
    db: aiosqlite.Connection,
    *,
    status: str | None = None,
) -> list[dict]:
    """List user jobs, optionally filtered by status."""
    if status:
        cursor = await db.execute(
            "SELECT * FROM user_jobs WHERE status = ? ORDER BY created_at DESC",
            (status,),
        )
    else:
        cursor = await db.execute(
            "SELECT * FROM user_jobs ORDER BY created_at DESC",
        )
    return [dict(r) for r in await cursor.fetchall()]


async def update_job(
    db: aiosqlite.Connection,
    job_id: str,
    *,
    status: str | None = None,
    last_run_at: str | None = None,
    last_status: str | None = None,
    last_result_json: str | None = None,
    next_run_at: str | None = None,
    failure_count: int | None = None,
) -> bool:
    """Update fields on a job. Returns True if a row was updated."""
    updates: list[str] = []
    params: list = []

    if status is not None:
        updates.append("status = ?")
        params.append(status)
    if last_run_at is not None:
        updates.append("last_run_at = ?")
        params.append(last_run_at)
    if last_status is not None:
        updates.append("last_status = ?")
        params.append(last_status)
    if last_result_json is not None:
        updates.append("last_result_json = ?")
        params.append(last_result_json)
    if next_run_at is not None:
        updates.append("next_run_at = ?")
        params.append(next_run_at)
    if failure_count is not None:
        updates.append("failure_count = ?")
        params.append(failure_count)

    if not updates:
        return False

    updates.append("updated_at = datetime('now')")
    params.append(job_id)
    cursor = await db.execute(
        f"UPDATE user_jobs SET {', '.join(updates)} WHERE id = ?",
        tuple(params),
    )
    await db.commit()
    return cursor.rowcount > 0


async def delete_job(db: aiosqlite.Connection, job_id: str) -> bool:
    """Delete a user job and its run history. Returns True if deleted."""
    await db.execute("DELETE FROM user_job_runs WHERE job_id = ?", (job_id,))
    cursor = await db.execute("DELETE FROM user_jobs WHERE id = ?", (job_id,))
    await db.commit()
    return cursor.rowcount > 0


# ── Run history ──────────────────────────────────────────────────────────────


async def record_run_start(
    db: aiosqlite.Connection,
    *,
    job_id: str,
    session_id: str | None = None,
) -> str:
    """Record a new run starting. Returns the run ID."""
    run_id = str(uuid.uuid4())
    now = datetime.now(UTC).isoformat()
    await db.execute(
        """INSERT INTO user_job_runs (id, job_id, status, session_id, started_at)
           VALUES (?, ?, 'running', ?, ?)""",
        (run_id, job_id, session_id, now),
    )
    await update_job(db, job_id, last_run_at=now, last_status="running")
    return run_id


async def attach_run_session(
    db: aiosqlite.Connection,
    run_id: str,
    session_id: str,
) -> bool:
    """Attach a spawned session to a run by ID."""
    cursor = await db.execute(
        "UPDATE user_job_runs SET session_id = ? WHERE id = ?",
        (session_id, run_id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def get_running_run_by_session(
    db: aiosqlite.Connection,
    session_id: str,
) -> dict | None:
    """Return the running user-job run attached to a session, if any."""
    if not session_id:
        return None
    cursor = await db.execute(
        """SELECT * FROM user_job_runs
           WHERE session_id = ? AND status = 'running'
           ORDER BY started_at DESC LIMIT 1""",
        (session_id,),
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def list_running_runs(db: aiosqlite.Connection) -> list[dict]:
    """Return every unfinished run, oldest first for boot reconciliation."""
    cursor = await db.execute(
        "SELECT * FROM user_job_runs WHERE status = 'running' ORDER BY started_at ASC",
    )
    return [dict(row) for row in await cursor.fetchall()]


async def record_run_complete(
    db: aiosqlite.Connection,
    run_id: str,
    *,
    status: str,
    result_json: dict | None = None,
    error_message: str | None = None,
    only_if_running: bool = False,
) -> bool:
    """Record a run completion; optionally compare-and-set from running.

    The parent job's summary is then recomputed from its runs by
    :func:`refresh_job_summary`, so overlapping runs cannot overwrite the newest
    run's status or lose a failure. If a completion saves the run but fails
    before the parent, boot reconciliation repairs the job (it refreshes every
    job still marked running); a compare-and-set call that finds the run already
    finished also refreshes the parent, so any repeat completion repairs it too.
    """
    now = datetime.now(UTC).isoformat()
    result_json_text = json.dumps(result_json) if result_json else None
    guard = " AND status = 'running'" if only_if_running else ""
    cursor = await db.execute(
        """UPDATE user_job_runs SET status = ?, completed_at = ?,
           result_json = ?, error_message = ? WHERE id = ?""" + guard,
        (status, now, result_json_text, error_message, run_id),
    )
    updated = cursor.rowcount > 0
    cursor = await db.execute(
        "SELECT job_id FROM user_job_runs WHERE id = ?", (run_id,),
    )
    row = await cursor.fetchone()
    if row:
        await refresh_job_summary(db, row[0])
    await db.commit()
    return updated or not only_if_running


async def refresh_job_summary(db: aiosqlite.Connection, job_id: str | None = None) -> None:
    """Recompute a job's latest-run summary and failure count from its runs.

    One statement, idempotent, and correct whatever order runs finished in:
    ``last_status`` is the latest-started run's status, ``last_result_json`` its
    result (kept when that run has none yet), and ``failure_count`` the number of
    failed runs started after the latest passed one. ``job_id=None`` is the boot
    repair: it refreshes every job still marked running, which is what a
    completion that saved its run but not its parent leaves behind, and leaves
    other jobs alone (a dispatch that never started a run marks its job failed
    with no run row to recompute from). Does not commit.
    """
    latest = (
        "SELECT {col} FROM user_job_runs r WHERE r.job_id = user_jobs.id"
        " ORDER BY r.started_at DESC, r.id DESC LIMIT 1"
    )
    where, params = (
        ("WHERE id = ?", (job_id,)) if job_id is not None
        else ("WHERE last_status = 'running'", ())
    )
    await db.execute(
        f"""UPDATE user_jobs SET
              last_status = COALESCE(({latest.format(col="status")}), last_status),
              last_result_json = COALESCE(({latest.format(col="result_json")}),
                                          last_result_json),
              failure_count = (
                SELECT COUNT(*) FROM user_job_runs f
                WHERE f.job_id = user_jobs.id AND f.status = 'failed'
                  AND f.started_at > COALESCE((
                    SELECT MAX(p.started_at) FROM user_job_runs p
                    WHERE p.job_id = user_jobs.id AND p.status = 'passed'), '')),
              updated_at = datetime('now')
            {where}""",
        params,
    )


async def get_run_history(
    db: aiosqlite.Connection,
    job_id: str,
    *,
    limit: int = 10,
) -> list[dict]:
    """Return recent runs for a job, newest first."""
    cursor = await db.execute(
        "SELECT * FROM user_job_runs WHERE job_id = ? ORDER BY started_at DESC LIMIT ?",
        (job_id, limit),
    )
    return [dict(r) for r in await cursor.fetchall()]
