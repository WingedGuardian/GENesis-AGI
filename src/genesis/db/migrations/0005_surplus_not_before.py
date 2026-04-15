"""Add not_before column to surplus_tasks for time-based scheduling.

Allows follow-up dispatcher and other systems to enqueue tasks that
should not be picked up until a specific time.
"""

from __future__ import annotations

import aiosqlite


async def up(db: aiosqlite.Connection) -> None:
    await db.execute(
        "ALTER TABLE surplus_tasks ADD COLUMN not_before TEXT"
    )


async def down(db: aiosqlite.Connection) -> None:
    # SQLite doesn't support DROP COLUMN on older versions; column is harmless.
    pass
