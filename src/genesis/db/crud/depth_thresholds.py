"""CRUD operations for depth_thresholds table."""

from __future__ import annotations

import aiosqlite


async def get(db: aiosqlite.Connection, depth_name: str) -> dict | None:
    cursor = await db.execute(
        "SELECT * FROM depth_thresholds WHERE depth_name = ?", (depth_name,)
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def list_all(db: aiosqlite.Connection) -> list[dict]:
    cursor = await db.execute(
        "SELECT * FROM depth_thresholds ORDER BY depth_name"
    )
    return [dict(r) for r in await cursor.fetchall()]


async def update_threshold(
    db: aiosqlite.Connection, depth_name: str, *, new_threshold: float
) -> bool:
    cursor = await db.execute(
        "UPDATE depth_thresholds SET threshold = ? WHERE depth_name = ?",
        (new_threshold, depth_name),
    )
    await db.commit()
    return cursor.rowcount > 0
