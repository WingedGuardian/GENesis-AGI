"""Add scenario column to procedural_memory.

The 'scenario' field stores the "when to use this" trigger condition
(ReMe's omega) for procedure retrieval. Nullable — existing procedures
keep working with NULL scenario.
"""

from __future__ import annotations

import aiosqlite


async def up(db: aiosqlite.Connection) -> None:
    # Table may not exist if migrations run on a fresh DB before schema init.
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='procedural_memory'"
    )
    if not await cursor.fetchone():
        return

    # Only add column if it doesn't already exist.
    col_cursor = await db.execute("PRAGMA table_info(procedural_memory)")
    cols = {row[1] for row in await col_cursor.fetchall()}
    if "scenario" not in cols:
        await db.execute(
            "ALTER TABLE procedural_memory ADD COLUMN scenario TEXT"
        )
