"""Add verification columns to follow_ups table.

Tracks whether a completed follow-up's linked task actually produced
output, enabling post-execution quality auditing.
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

    if "verified_at" not in cols:
        await db.execute("ALTER TABLE follow_ups ADD COLUMN verified_at TEXT")
    if "verification_notes" not in cols:
        await db.execute("ALTER TABLE follow_ups ADD COLUMN verification_notes TEXT")
