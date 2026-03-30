"""CRUD operations for capability_gaps table."""

from __future__ import annotations

import aiosqlite


async def create(
    db: aiosqlite.Connection,
    *,
    id: str,
    description: str,
    gap_type: str,
    first_seen: str,
    last_seen: str,
    task_context: str | None = None,
    blocker_class: str | None = None,
    feasibility: str | None = None,
) -> str:
    await db.execute(
        """INSERT INTO capability_gaps
           (id, description, task_context, gap_type, blocker_class, feasibility,
            first_seen, last_seen)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (id, description, task_context, gap_type, blocker_class, feasibility,
         first_seen, last_seen),
    )
    await db.commit()
    return id


async def upsert(
    db: aiosqlite.Connection,
    *,
    id: str,
    description: str,
    gap_type: str,
    first_seen: str,
    last_seen: str,
    task_context: str | None = None,
    blocker_class: str | None = None,
    feasibility: str | None = None,
) -> str:
    """Idempotent write: insert or update last_seen/frequency on conflict."""
    await db.execute(
        """INSERT INTO capability_gaps
           (id, description, task_context, gap_type, blocker_class, feasibility,
            first_seen, last_seen)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET
             description = excluded.description, task_context = excluded.task_context,
             gap_type = excluded.gap_type, blocker_class = excluded.blocker_class,
             feasibility = excluded.feasibility, last_seen = excluded.last_seen,
             frequency = frequency + 1""",
        (id, description, task_context, gap_type, blocker_class, feasibility,
         first_seen, last_seen),
    )
    await db.commit()
    return id


async def get_by_id(db: aiosqlite.Connection, id: str) -> dict | None:
    cursor = await db.execute("SELECT * FROM capability_gaps WHERE id = ?", (id,))
    row = await cursor.fetchone()
    return dict(row) if row else None


async def list_open(db: aiosqlite.Connection, *, limit: int = 50) -> list[dict]:
    cursor = await db.execute(
        "SELECT * FROM capability_gaps WHERE status = 'open' "
        "ORDER BY frequency DESC LIMIT ?",
        (limit,),
    )
    return [dict(r) for r in await cursor.fetchall()]


async def increment_frequency(
    db: aiosqlite.Connection, id: str, *, last_seen: str
) -> bool:
    cursor = await db.execute(
        "UPDATE capability_gaps SET frequency = frequency + 1, last_seen = ? WHERE id = ?",
        (last_seen, id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def resolve(
    db: aiosqlite.Connection, id: str, *, resolved_at: str, resolution_notes: str
) -> bool:
    cursor = await db.execute(
        "UPDATE capability_gaps SET status = 'resolved', resolved_at = ?, resolution_notes = ? "
        "WHERE id = ?",
        (resolved_at, resolution_notes, id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def delete(db: aiosqlite.Connection, id: str) -> bool:
    cursor = await db.execute("DELETE FROM capability_gaps WHERE id = ?", (id,))
    await db.commit()
    return cursor.rowcount > 0
