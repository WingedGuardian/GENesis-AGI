"""CRUD operations for task_steps table --- per-step state for background tasks."""

from __future__ import annotations

import aiosqlite


async def create_step(
    db: aiosqlite.Connection,
    *,
    task_id: str,
    step_idx: int,
    step_type: str = "code",
    description: str = "",
    status: str = "pending",
) -> None:
    """Insert a new step row. Idempotent via INSERT OR IGNORE."""
    await db.execute(
        """INSERT OR IGNORE INTO task_steps
           (task_id, step_idx, step_type, description, status)
           VALUES (?, ?, ?, ?, ?)""",
        (task_id, step_idx, step_type, description, status),
    )
    await db.commit()


async def update_step(
    db: aiosqlite.Connection,
    task_id: str,
    step_idx: int,
    *,
    status: str | None = None,
    result_json: str | None = None,
    cost_usd: float | None = None,
    model_used: str | None = None,
    session_id: str | None = None,
    started_at: str | None = None,
    completed_at: str | None = None,
) -> bool:
    """Update fields on an existing step row. Returns True if a row was updated."""
    updates: list[str] = []
    params: list = []
    if status is not None:
        updates.append("status = ?")
        params.append(status)
    if result_json is not None:
        updates.append("result_json = ?")
        params.append(result_json)
    if cost_usd is not None:
        updates.append("cost_usd = ?")
        params.append(cost_usd)
    if model_used is not None:
        updates.append("model_used = ?")
        params.append(model_used)
    if session_id is not None:
        updates.append("session_id = ?")
        params.append(session_id)
    if started_at is not None:
        updates.append("started_at = ?")
        params.append(started_at)
    if completed_at is not None:
        updates.append("completed_at = ?")
        params.append(completed_at)
    if not updates:
        return False
    params.extend([task_id, step_idx])
    cursor = await db.execute(
        f"UPDATE task_steps SET {', '.join(updates)} "
        "WHERE task_id = ? AND step_idx = ?",
        tuple(params),
    )
    await db.commit()
    return cursor.rowcount > 0


async def get_steps_for_task(
    db: aiosqlite.Connection,
    task_id: str,
) -> list[dict]:
    """Return all steps for a task, ordered by step_idx."""
    cursor = await db.execute(
        "SELECT * FROM task_steps WHERE task_id = ? ORDER BY step_idx",
        (task_id,),
    )
    return [dict(r) for r in await cursor.fetchall()]


async def get_last_completed_step(
    db: aiosqlite.Connection,
    task_id: str,
) -> dict | None:
    """Return the highest-index step with status='completed', or None."""
    cursor = await db.execute(
        """SELECT * FROM task_steps
           WHERE task_id = ? AND status = 'completed'
           ORDER BY step_idx DESC LIMIT 1""",
        (task_id,),
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def get_incomplete_steps(
    db: aiosqlite.Connection,
    task_id: str,
) -> list[dict]:
    """Return all steps for a task that are NOT 'completed', ordered by step_idx."""
    cursor = await db.execute(
        """SELECT * FROM task_steps
           WHERE task_id = ? AND status != 'completed'
           ORDER BY step_idx""",
        (task_id,),
    )
    return [dict(r) for r in await cursor.fetchall()]
