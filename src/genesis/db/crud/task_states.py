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
    source: str = "user",
    created_at: str | None = None,
) -> str:
    await db.execute(
        """INSERT INTO task_states
           (task_id, description, current_phase, decisions, blockers,
            outputs, session_id, intake_token, source, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, datetime('now')),
                   COALESCE(?, datetime('now')))""",
        (task_id, description, current_phase, decisions, blockers,
         outputs, session_id, intake_token, source, created_at, created_at),
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


async def claim_for_dispatch(db: aiosqlite.Connection, task_id: str) -> bool:
    """Atomically claim a PENDING task for dispatch (at-most-once).

    Transitions ``current_phase`` from ``pending`` to ``dispatching`` in one
    guarded UPDATE, but ONLY for a row still in ``pending``. Returns True iff
    this call won the claim (``rowcount == 1``). A row already claimed
    (``dispatching``), running, or terminal does not match and returns False,
    so the caller must refuse it — this is the dedup gate that stops two
    overlapping dispatches from both executing the same task.

    Stamps ``updated_at`` (ISO) so :func:`recover_stale_dispatching` can reap a
    claim stranded by a crash between the claim and the first real phase.
    """
    now = datetime.now(UTC).isoformat()
    cursor = await db.execute(
        """UPDATE task_states
           SET current_phase = 'dispatching', updated_at = ?
           WHERE task_id = ? AND current_phase = 'pending'""",
        (now, task_id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def recover_stale_dispatching(
    db: aiosqlite.Connection, max_age_s: int = 120,
) -> int:
    """Reset ``dispatching`` claims older than ``max_age_s`` back to ``pending``.

    A task sits in ``dispatching`` only between the atomic claim and the first
    real phase transition. If the executor crashes in that window the row would
    stay stuck (and be refused on the next pickup), so this reaper — run at
    startup and on the dispatch poll — returns it to ``pending`` for a clean
    re-dispatch. Mirrors ``direct_session_queue.recover_stale_claims``. All
    ``dispatching`` rows carry an ISO ``updated_at`` (written by
    :func:`claim_for_dispatch`), so the ``<`` compare is well-ordered.
    """
    cutoff_iso = (datetime.now(UTC) - timedelta(seconds=max_age_s)).isoformat()
    now = datetime.now(UTC).isoformat()
    cursor = await db.execute(
        """UPDATE task_states
           SET current_phase = 'pending', updated_at = ?
           WHERE current_phase = 'dispatching' AND updated_at < ?""",
        (now, cutoff_iso),
    )
    await db.commit()
    return cursor.rowcount


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
