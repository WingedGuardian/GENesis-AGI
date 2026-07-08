"""Add idx_dead_letter_created — index dead_letter(created_at).

Backs the rate-based provider-exhaustion storm detector
(``dead_letter.count_recent`` over a rolling window), which runs on every
awareness tick. The dead_letter table is not pruned on a schedule, so it grows
unbounded over months; without this index the storm counter degrades into a
full-table scan. Additive + idempotent; canonical DDL mirrored in
``db/schema/_tables.py`` for the fresh-DB path.
"""

from __future__ import annotations

import aiosqlite


async def up(db: aiosqlite.Connection) -> None:
    # Guard for the base table: fresh/test DBs get the index from the canonical
    # DDL in db/schema/_tables.py, and the runner's lifecycle tests apply
    # migrations to a bare DB where dead_letter does not yet exist.
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='dead_letter'"
    )
    if await cursor.fetchone():
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_dead_letter_created "
            "ON dead_letter(created_at)"
        )


async def down(db: aiosqlite.Connection) -> None:
    await db.execute("DROP INDEX IF EXISTS idx_dead_letter_created")
