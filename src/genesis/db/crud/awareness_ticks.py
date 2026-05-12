"""CRUD operations for awareness_ticks table."""

from __future__ import annotations

import aiosqlite


async def create(
    db: aiosqlite.Connection,
    *,
    id: str,
    source: str,
    signals_json: str,
    scores_json: str,
    created_at: str,
    classified_depth: str | None = None,
    trigger_reason: str | None = None,
    dispatched: int = 0,
) -> str:
    await db.execute(
        """INSERT INTO awareness_ticks
           (id, source, signals_json, scores_json, classified_depth,
            trigger_reason, created_at, dispatched)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (id, source, signals_json, scores_json, classified_depth,
         trigger_reason, created_at, dispatched),
    )
    await db.commit()
    return id


async def get_by_id(db: aiosqlite.Connection, id: str) -> dict | None:
    cursor = await db.execute(
        "SELECT * FROM awareness_ticks WHERE id = ?", (id,)
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def query(
    db: aiosqlite.Connection,
    *,
    source: str | None = None,
    classified_depth: str | None = None,
    limit: int = 50,
) -> list[dict]:
    sql = "SELECT * FROM awareness_ticks WHERE 1=1"
    params: list = []
    if source is not None:
        sql += " AND source = ?"
        params.append(source)
    if classified_depth is not None:
        sql += " AND classified_depth = ?"
        params.append(classified_depth)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    cursor = await db.execute(sql, params)
    return [dict(r) for r in await cursor.fetchall()]


async def count_in_window(
    db: aiosqlite.Connection,
    *,
    depth: str,
    window_seconds: int,
    dispatched_only: bool = False,
) -> int:
    """Count ticks at a given depth within the last window_seconds.

    When *dispatched_only* is True, only ticks where the reflection was
    actually dispatched (not throttled/failed) are counted.
    """
    sql = (
        "SELECT COUNT(*) as cnt FROM awareness_ticks "
        "WHERE classified_depth = ? "
        "AND created_at >= strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now', ?)"
    )
    params: list = [depth, f"-{window_seconds} seconds"]
    if dispatched_only:
        sql += " AND dispatched = 1"
    cursor = await db.execute(sql, params)
    row = await cursor.fetchone()
    return row["cnt"] if row else 0


async def last_at_depth(
    db: aiosqlite.Connection,
    depth: str,
    *,
    dispatched_only: bool = False,
) -> dict | None:
    """Get the most recent tick at a given depth.

    When *dispatched_only* is True, only ticks where the reflection was
    actually dispatched (not throttled/failed) are returned.
    """
    sql = (
        "SELECT * FROM awareness_ticks "
        "WHERE classified_depth = ?"
    )
    params: list = [depth]
    if dispatched_only:
        sql += " AND dispatched = 1"
    sql += " ORDER BY created_at DESC LIMIT 1"
    cursor = await db.execute(sql, params)
    row = await cursor.fetchone()
    return dict(row) if row else None


async def mark_dispatched(db: aiosqlite.Connection, tick_id: str) -> bool:
    """Mark a tick as successfully dispatched (reflection ran)."""
    cursor = await db.execute(
        "UPDATE awareness_ticks SET dispatched = 1 WHERE id = ?",
        (tick_id,),
    )
    await db.commit()
    return cursor.rowcount > 0


async def last_tick(db: aiosqlite.Connection) -> dict | None:
    """Get the most recent tick regardless of depth."""
    cursor = await db.execute(
        "SELECT * FROM awareness_ticks ORDER BY created_at DESC LIMIT 1",
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def count_in_window_all(
    db: aiosqlite.Connection, *, window_seconds: int
) -> int:
    """Count all ticks within the last window_seconds."""
    cursor = await db.execute(
        """SELECT COUNT(*) as cnt FROM awareness_ticks
           WHERE created_at >= strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now', ?)""",
        (f"-{window_seconds} seconds",),
    )
    row = await cursor.fetchone()
    return row["cnt"] if row else 0


async def count_by_source(
    db: aiosqlite.Connection, *, source: str, window_seconds: int
) -> int:
    """Count ticks by source within the last window_seconds."""
    cursor = await db.execute(
        """SELECT COUNT(*) as cnt FROM awareness_ticks
           WHERE source = ? AND created_at >= strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now', ?)""",
        (source, f"-{window_seconds} seconds"),
    )
    row = await cursor.fetchone()
    return row["cnt"] if row else 0


async def last_reflected_tick(db: aiosqlite.Connection) -> dict | None:
    """Get the most recent tick where a reflection was triggered (any depth)."""
    cursor = await db.execute(
        """SELECT * FROM awareness_ticks
           WHERE classified_depth IS NOT NULL
           ORDER BY created_at DESC LIMIT 1""",
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def delete(db: aiosqlite.Connection, id: str) -> bool:
    cursor = await db.execute(
        "DELETE FROM awareness_ticks WHERE id = ?", (id,)
    )
    await db.commit()
    return cursor.rowcount > 0
