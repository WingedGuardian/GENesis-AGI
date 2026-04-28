"""Add pinned column to follow_ups table.

Pinned follow-ups are user-tracked items that the ego can see and think
about but cannot auto-resolve. Only the user can close a pinned follow-up.
"""

from __future__ import annotations

import aiosqlite


async def up(db: aiosqlite.Connection) -> None:
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='follow_ups'"
    )
    if not await cursor.fetchone():
        return

    col_cursor = await db.execute("PRAGMA table_info(follow_ups)")
    cols = {row[1] for row in await col_cursor.fetchall()}

    if "pinned" not in cols:
        await db.execute(
            "ALTER TABLE follow_ups ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0"
        )
