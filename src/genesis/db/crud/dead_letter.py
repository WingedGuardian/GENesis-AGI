"""CRUD operations for dead_letter table."""

from __future__ import annotations

import aiosqlite


async def create(
    db: aiosqlite.Connection,
    *,
    id: str,
    operation_type: str,
    payload: str,
    target_provider: str,
    failure_reason: str,
    created_at: str,
    status: str = "pending",
) -> str:
    await db.execute(
        """INSERT INTO dead_letter
           (id, operation_type, payload, target_provider, failure_reason, created_at, status)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (id, operation_type, payload, target_provider, failure_reason, created_at, status),
    )
    await db.commit()
    return id


async def get_by_id(db: aiosqlite.Connection, id: str) -> dict | None:
    cursor = await db.execute("SELECT * FROM dead_letter WHERE id = ?", (id,))
    row = await cursor.fetchone()
    return dict(row) if row else None


async def query_pending(
    db: aiosqlite.Connection,
    *,
    target_provider: str | None = None,
    limit: int = 50,
) -> list[dict]:
    sql = "SELECT * FROM dead_letter WHERE status = 'pending'"
    params: list = []
    if target_provider is not None:
        sql += " AND target_provider = ?"
        params.append(target_provider)
    sql += " ORDER BY created_at ASC LIMIT ?"
    params.append(limit)
    cursor = await db.execute(sql, params)
    return [dict(r) for r in await cursor.fetchall()]


async def update_status(db: aiosqlite.Connection, id: str, *, status: str) -> bool:
    cursor = await db.execute(
        "UPDATE dead_letter SET status = ? WHERE id = ?", (status, id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def increment_retry(db: aiosqlite.Connection, id: str, *, last_retry_at: str) -> bool:
    cursor = await db.execute(
        "UPDATE dead_letter SET retry_count = retry_count + 1, last_retry_at = ? WHERE id = ?",
        (last_retry_at, id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def count_pending(
    db: aiosqlite.Connection,
    *,
    target_provider: str | None = None,
) -> int:
    sql = "SELECT COUNT(*) FROM dead_letter WHERE status = 'pending'"
    params: list = []
    if target_provider is not None:
        sql += " AND target_provider = ?"
        params.append(target_provider)
    cursor = await db.execute(sql, params)
    row = await cursor.fetchone()
    return int(row[0])


async def delete(db: aiosqlite.Connection, id: str) -> bool:
    cursor = await db.execute("DELETE FROM dead_letter WHERE id = ?", (id,))
    await db.commit()
    return cursor.rowcount > 0


async def query_recent(
    db: aiosqlite.Connection,
    *,
    since: str | None = None,
    target_provider: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Return recent dead letter items (any status) within a time window."""
    sql = "SELECT * FROM dead_letter"
    clauses: list[str] = []
    params: list = []
    if since:
        clauses.append("created_at >= ?")
        params.append(since)
    if target_provider:
        clauses.append("target_provider = ?")
        params.append(target_provider)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(sql, params)
    return [dict(r) for r in await cursor.fetchall()]
