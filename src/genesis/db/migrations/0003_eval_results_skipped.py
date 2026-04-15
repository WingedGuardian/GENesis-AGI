"""Add skipped column to eval_results for distinguishing quota skips from failures."""

from __future__ import annotations

import aiosqlite


async def up(db: aiosqlite.Connection) -> None:
    await db.execute(
        "ALTER TABLE eval_results ADD COLUMN skipped INTEGER DEFAULT 0"
    )


async def down(db: aiosqlite.Connection) -> None:
    # SQLite doesn't support DROP COLUMN on older versions; recreate is complex.
    # Just leave the column — it's harmless.
    pass
