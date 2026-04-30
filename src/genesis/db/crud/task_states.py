"""CRUD operations for task_states table."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import aiosqlite


async def create(
    db: aiosqlite.Connection,
    *,
    task_id: str,
    description: str,
    current_phase: str = "planning",
    decisions: str | None = None,
    blockers: str | None = None,
    outputs: str | None = None,
    session_id: str | None = None,
    intake_token: str | None = None,
    created_at: str | None = None,
) -> str:
    await db.execute(
        """INSERT INTO task_states
           (task_id, description, current_phase, decisions, blockers,
            outputs, session_id, intake_token, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, datetime('now')),
                   COALESCE(?, datetime('now')))""",
        (task_id, description, current_phase, decisions, blockers,
         outputs, session_id, intake_token, created_at, created_at),
    )
    await db.commit()
    return task_id


async def get_by_id(db: aiosqlite.Connection, task_id: str) -> dict | None:
    cursor = await db.execute(
        "SELECT * FROM task_states WHERE task_id = ?", (task_id,)
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def get_by_session(
    db: aiosqlite.Connection, session_id: str
) -> list[dict]:
    cursor = await db.execute(
        """SELECT * FROM task_states
           WHERE session_id = ?
           ORDER BY created_at DESC""",
        (session_id,),
    )
    return [dict(r) for r in await cursor.fetchall()]


async def update(
    db: aiosqlite.Connection,
    task_id: str,
    *,
    current_phase: str | None = None,
    decisions: str | None = None,
    blockers: str | None = None,
    outputs: str | None = None,
    updated_at: str | None = None,
) -> bool:
    updates = []
    params: list = []
    if current_phase is not None:
        updates.append("current_phase = ?")
        params.append(current_phase)
    if decisions is not None:
        updates.append("decisions = ?")
        params.append(decisions)
    if blockers is not None:
        updates.append("blockers = ?")
        params.append(blockers)
    if outputs is not None:
        updates.append("outputs = ?")
        params.append(outputs)
    if updated_at is not None:
        updates.append("updated_at = ?")
        params.append(updated_at)
    if not updates:
        return False
    params.append(task_id)
    cursor = await db.execute(
        f"UPDATE task_states SET {', '.join(updates)} WHERE task_id = ?",
        tuple(params),
    )
    await db.commit()
    return cursor.rowcount > 0


async def delete(db: aiosqlite.Connection, task_id: str) -> bool:
    cursor = await db.execute(
        "DELETE FROM task_states WHERE task_id = ?", (task_id,)
    )
    await db.commit()
    return cursor.rowcount > 0


async def list_by_phase(
    db: aiosqlite.Connection, phase: str
) -> list[dict]:
    """Return all tasks in the given phase, newest first."""
    cursor = await db.execute(
        """SELECT * FROM task_states
           WHERE current_phase = ?
           ORDER BY updated_at DESC""",
        (phase,),
    )
    return [dict(r) for r in await cursor.fetchall()]


async def list_active(db: aiosqlite.Connection) -> list[dict]:
    """Return all tasks not in a terminal phase, newest first."""
    terminal = ("completed", "failed", "cancelled")
    placeholders = ", ".join("?" for _ in terminal)
    cursor = await db.execute(
        f"""SELECT * FROM task_states
            WHERE current_phase NOT IN ({placeholders})
            ORDER BY updated_at DESC""",
        terminal,
    )
    return [dict(r) for r in await cursor.fetchall()]


async def list_all_recent(
    db: aiosqlite.Connection, *, limit: int = 50
) -> list[dict]:
    """Return the most recent tasks regardless of phase."""
    cursor = await db.execute(
        "SELECT * FROM task_states ORDER BY updated_at DESC LIMIT ?",
        (limit,),
    )
    return [dict(r) for r in await cursor.fetchall()]


async def create_intake_token(db: aiosqlite.Connection) -> str:
    """Generate an intake token for task submission.

    Inserts a valid token into intake_tokens and returns it.
    The token expires in 2 hours.
    """
    token = uuid.uuid4().hex
    now = datetime.now(UTC).isoformat()
    expires = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
    await db.execute(
        "INSERT INTO intake_tokens (token, created_at, expires_at) VALUES (?,?,?)",
        (token, now, expires),
    )
    await db.commit()
    return token
