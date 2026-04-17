"""Add not_before column to surplus_tasks for time-based scheduling.

Allows follow-up dispatcher and other systems to enqueue tasks that
should not be picked up until a specific time.
"""

from __future__ import annotations

import aiosqlite


async def up(db: aiosqlite.Connection) -> None:
    # Table may not exist if migrations run on a fresh DB before schema init.
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='surplus_tasks'"
    )
    if await cursor.fetchone():
        # Only add column if table exists and column doesn't
        col_cursor = await db.execute("PRAGMA table_info(surplus_tasks)")
        cols = {row[1] for row in await col_cursor.fetchall()}
        if "not_before" not in cols:
            await db.execute(
                "ALTER TABLE surplus_tasks ADD COLUMN not_before TEXT"
            )


async def down(db: aiosqlite.Connection) -> None:
    # SQLite doesn't support DROP COLUMN on older versions; column is harmless.
    pass
