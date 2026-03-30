"""CRUD operations for drive_weights table."""

from __future__ import annotations

import aiosqlite


async def get_all(db: aiosqlite.Connection) -> list[dict]:
    cursor = await db.execute("SELECT * FROM drive_weights ORDER BY drive_name")
    return [dict(r) for r in await cursor.fetchall()]


async def get_weight(db: aiosqlite.Connection, drive_name: str) -> float | None:
    cursor = await db.execute(
        "SELECT current_weight FROM drive_weights WHERE drive_name = ?",
        (drive_name,),
    )
    row = await cursor.fetchone()
    return row[0] if row else None


async def update_weight(
    db: aiosqlite.Connection,
    drive_name: str,
    new_weight: float,
) -> bool:
    """Update current_weight, clamping to [min_weight, max_weight]."""
    cursor = await db.execute(
        """UPDATE drive_weights
           SET current_weight = MAX(min_weight, MIN(max_weight, ?))
           WHERE drive_name = ?""",
        (new_weight, drive_name),
    )
    await db.commit()
    return cursor.rowcount > 0


async def adapt_weight(
    db: aiosqlite.Connection,
    drive_name: str,
    delta: float,
) -> float | None:
    """Apply EMA-style delta to a drive weight, respecting bounds.

    Returns the new weight, or None if drive not found.
    """
    row_cur = await db.execute(
        "SELECT current_weight, min_weight, max_weight FROM drive_weights WHERE drive_name = ?",
        (drive_name,),
    )
    row = await row_cur.fetchone()
    if not row:
        return None
    current, min_w, max_w = row[0], row[1], row[2]
    new_weight = max(min_w, min(max_w, current + delta))
    await db.execute(
        "UPDATE drive_weights SET current_weight = ? WHERE drive_name = ?",
        (new_weight, drive_name),
    )
    await db.commit()
    return new_weight
