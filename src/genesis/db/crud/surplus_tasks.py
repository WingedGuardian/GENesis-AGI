"""CRUD operations for surplus_tasks table."""

from __future__ import annotations

import aiosqlite


async def create(
    db: aiosqlite.Connection,
    *,
    id: str,
    task_type: str,
    compute_tier: str,
    priority: float,
    drive_alignment: str,
    created_at: str,
    payload: str | None = None,
    status: str = "pending",
    not_before: str | None = None,
) -> str:
    await db.execute(
        """INSERT INTO surplus_tasks
           (id, task_type, compute_tier, priority, drive_alignment,
            status, payload, created_at, not_before)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (id, task_type, compute_tier, priority, drive_alignment,
         status, payload, created_at, not_before),
    )
    await db.commit()
    return id


async def get_by_id(db: aiosqlite.Connection, id: str) -> dict | None:
    cursor = await db.execute("SELECT * FROM surplus_tasks WHERE id = ?", (id,))
    row = await cursor.fetchone()
    return dict(row) if row else None


async def next_task(
    db: aiosqlite.Connection,
    *,
    available_tiers: list[str],
) -> dict | None:
    placeholders = ",".join("?" for _ in available_tiers)
    cursor = await db.execute(
        f"SELECT * FROM surplus_tasks WHERE status = 'pending' "
        f"AND compute_tier IN ({placeholders}) "
        f"AND (not_before IS NULL OR not_before <= strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')) "
        f"ORDER BY priority DESC, created_at ASC LIMIT 1",
        available_tiers,
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def mark_running(
    db: aiosqlite.Connection,
    id: str,
    *,
    started_at: str,
) -> bool:
    cursor = await db.execute(
        "UPDATE surplus_tasks SET status = 'running', started_at = ? WHERE id = ?",
        (started_at, id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def mark_completed(
    db: aiosqlite.Connection,
    id: str,
    *,
    completed_at: str,
    result_staging_id: str | None = None,
) -> bool:
    cursor = await db.execute(
        "UPDATE surplus_tasks SET status = 'completed', completed_at = ?, "
        "result_staging_id = ? WHERE id = ?",
        (completed_at, result_staging_id, id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def mark_failed(
    db: aiosqlite.Connection,
    id: str,
    *,
    failure_reason: str,
) -> bool:
    cursor = await db.execute(
        "UPDATE surplus_tasks SET status = 'failed', failure_reason = ?, "
        "attempt_count = attempt_count + 1 WHERE id = ?",
        (failure_reason, id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def recover_stuck(
    db: aiosqlite.Connection,
    *,
    older_than_hours: int = 2,
) -> tuple[int, int]:
    """Recover tasks stuck in 'running' state beyond the timeout threshold."""
    return await recover_stuck_with_retries(db, older_than_hours=older_than_hours, max_retries=3)


async def recover_stuck_with_retries(
    db: aiosqlite.Connection,
    *,
    older_than_hours: int = 2,
    max_retries: int = 3,
) -> tuple[int, int]:
    """Recover tasks stuck in 'running' state.

    Re-queues retryable tasks and permanently fails exhausted tasks.
    Returns (requeued_count, failed_count).
    """
    import logging
    from datetime import UTC, datetime, timedelta

    logger = logging.getLogger(__name__)
    cutoff = (datetime.now(UTC) - timedelta(hours=older_than_hours)).isoformat()

    cursor = await db.execute(
        "UPDATE surplus_tasks SET status = 'pending', "
        "started_at = NULL, failure_reason = NULL, "
        "attempt_count = attempt_count + 1 "
        "WHERE status = 'running' AND started_at < ? AND attempt_count < ?",
        (cutoff, max_retries),
    )
    requeued = cursor.rowcount

    cursor = await db.execute(
        "UPDATE surplus_tasks SET status = 'failed', "
        "failure_reason = 'stuck: exceeded running timeout (max retries exhausted)' "
        "WHERE status = 'running' AND started_at < ? AND attempt_count >= ?",
        (cutoff, max_retries),
    )
    failed = cursor.rowcount
    await db.commit()

    if requeued > 0:
        logger.warning("Re-queued %d stuck surplus tasks for retry", requeued)
    if failed > 0:
        logger.error("Permanently failed %d stuck surplus tasks (max retries exhausted)", failed)

    return requeued, failed


async def drain_expired(db: aiosqlite.Connection, *, before: str) -> int:
    cursor = await db.execute(
        "DELETE FROM surplus_tasks WHERE status = 'pending' AND created_at < ?",
        (before,),
    )
    await db.commit()
    return cursor.rowcount


async def count_pending(db: aiosqlite.Connection) -> int:
    cursor = await db.execute(
        "SELECT COUNT(*) FROM surplus_tasks WHERE status = 'pending'",
    )
    row = await cursor.fetchone()
    return row[0]


async def count_pending_by_type(db: aiosqlite.Connection, task_type: str) -> int:
    cursor = await db.execute(
        "SELECT COUNT(*) FROM surplus_tasks WHERE status = 'pending' AND task_type = ?",
        (task_type,),
    )
    row = await cursor.fetchone()
    return row[0]


async def count_active_by_type(db: aiosqlite.Connection, task_type: str) -> int:
    """Count tasks that are pending OR running for a given type."""
    cursor = await db.execute(
        "SELECT COUNT(*) FROM surplus_tasks "
        "WHERE status IN ('pending', 'running') AND task_type = ?",
        (task_type,),
    )
    row = await cursor.fetchone()
    return row[0]


async def delete(db: aiosqlite.Connection, id: str) -> bool:
    cursor = await db.execute("DELETE FROM surplus_tasks WHERE id = ?", (id,))
    await db.commit()
    return cursor.rowcount > 0
