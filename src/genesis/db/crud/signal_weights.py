"""CRUD operations for signal_weights table."""

from __future__ import annotations

import json

import aiosqlite


async def get(db: aiosqlite.Connection, signal_name: str) -> dict | None:
    cursor = await db.execute(
        "SELECT * FROM signal_weights WHERE signal_name = ?", (signal_name,)
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def list_all(db: aiosqlite.Connection) -> list[dict]:
    cursor = await db.execute("SELECT * FROM signal_weights ORDER BY signal_name")
    return [dict(r) for r in await cursor.fetchall()]


async def list_by_depth(db: aiosqlite.Connection, depth: str) -> list[dict]:
    """Return signals that feed into a specific depth level."""
    cursor = await db.execute("SELECT * FROM signal_weights")
    rows = await cursor.fetchall()
    return [
        dict(r) for r in rows
        if depth in json.loads(r["feeds_depths"])
    ]


async def update_weight(
    db: aiosqlite.Connection, signal_name: str, *, new_weight: float
) -> bool:
    cursor = await db.execute(
        """UPDATE signal_weights
           SET current_weight = MAX(min_weight, MIN(max_weight, ?))
           WHERE signal_name = ?""",
        (new_weight, signal_name),
    )
    await db.commit()
    return cursor.rowcount > 0


async def reset_to_initial(db: aiosqlite.Connection, signal_name: str) -> bool:
    cursor = await db.execute(
        "UPDATE signal_weights SET current_weight = initial_weight WHERE signal_name = ?",
        (signal_name,),
    )
    await db.commit()
    return cursor.rowcount > 0
