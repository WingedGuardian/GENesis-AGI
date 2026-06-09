"""Add scenario column to procedural_memory.

The 'scenario' field stores the "when to use this" trigger condition
(ReMe's omega) for procedure retrieval. Nullable — existing procedures
keep working with NULL scenario.
"""

from __future__ import annotations

import aiosqlite


async def up(db: aiosqlite.Connection) -> None:
    await db.execute(
        "ALTER TABLE procedural_memory ADD COLUMN scenario TEXT"
    )
