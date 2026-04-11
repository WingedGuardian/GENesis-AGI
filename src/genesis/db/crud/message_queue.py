"""CRUD operations for message_queue table."""

from __future__ import annotations

import aiosqlite


async def create(
    db: aiosqlite.Connection,
    *,
    id: str,
    source: str,
    target: str,
    message_type: str,
    content: str,
    created_at: str,
    priority: str = "medium",
    task_id: str | None = None,
    session_id: str | None = None,
) -> str:
    await db.execute(
        """INSERT INTO message_queue
           (id, task_id, source, target, message_type, priority, content,
            session_id, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (id, task_id, source, target, message_type, priority, content,
         session_id, created_at),
    )
    await db.commit()
    return id


async def get_by_id(db: aiosqlite.Connection, id: str) -> dict | None:
    cursor = await db.execute("SELECT * FROM message_queue WHERE id = ?", (id,))
    row = await cursor.fetchone()
    return dict(row) if row else None


async def query_pending(
    db: aiosqlite.Connection,
    *,
    target: str | None = None,
    message_type: str | None = None,
    limit: int = 50,
) -> list[dict]:
    sql = "SELECT * FROM message_queue WHERE responded_at IS NULL AND expired_at IS NULL"
    params: list = []
    if target is not None:
        sql += " AND target = ?"
        params.append(target)
    if message_type is not None:
        sql += " AND message_type = ?"
        params.append(message_type)
    sql += " ORDER BY created_at ASC LIMIT ?"
    params.append(limit)
    cursor = await db.execute(sql, params)
    return [dict(r) for r in await cursor.fetchall()]


async def set_response(
    db: aiosqlite.Connection,
    id: str,
    *,
    response: str,
    responded_at: str,
) -> bool:
    cursor = await db.execute(
        "UPDATE message_queue SET response = ?, responded_at = ? WHERE id = ?",
        (response, responded_at, id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def set_expired(
    db: aiosqlite.Connection,
    id: str,
    *,
    expired_at: str,
) -> bool:
    cursor = await db.execute(
        "UPDATE message_queue SET expired_at = ? WHERE id = ?",
        (expired_at, id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def query_by_session(
    db: aiosqlite.Connection,
    session_id: str,
) -> list[dict]:
    cursor = await db.execute(
        "SELECT * FROM message_queue WHERE session_id = ? ORDER BY created_at ASC",
        (session_id,),
    )
    return [dict(r) for r in await cursor.fetchall()]


async def count_pending(
    db: aiosqlite.Connection,
    *,
    target: str | None = None,
) -> int:
    sql = "SELECT COUNT(*) FROM message_queue WHERE responded_at IS NULL AND expired_at IS NULL"
    params: list = []
    if target is not None:
        sql += " AND target = ?"
        params.append(target)
    cursor = await db.execute(sql, params)
    row = await cursor.fetchone()
    return int(row[0])


async def expire_older_than(
    db: aiosqlite.Connection,
    *,
    max_age_hours: int = 168,
    expired_at: str,
) -> int:
    """Expire pending messages older than max_age_hours (default 7 days).

    ``expired_at`` is used BOTH as the reference clock (cutoff = expired_at
    minus max_age_hours) AND as the stamp written to each expired row. This
    makes the function deterministic and testable. In production the caller
    passes ``datetime.now(UTC).isoformat()`` so behavior matches a "wall
    clock minus max_age_hours" cutoff.
    """
    cursor = await db.execute(
        """UPDATE message_queue SET expired_at = ?
           WHERE responded_at IS NULL AND expired_at IS NULL
             AND created_at < datetime(?, ? || ' hours')""",
        (expired_at, expired_at, f"-{max_age_hours}"),
    )
    await db.commit()
    return cursor.rowcount


async def delete(db: aiosqlite.Connection, id: str) -> bool:
    cursor = await db.execute("DELETE FROM message_queue WHERE id = ?", (id,))
    await db.commit()
    return cursor.rowcount > 0
