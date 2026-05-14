"""Reduce Deep reflection floor from 48h to 24h.

With surplus review removed from Deep (intake pipeline handles triage),
Deep cycles are lighter and faster. Reducing cadence from 48h to 24h
ensures patterns and contradictions are caught more promptly.

Idempotent — uses UPDATE with WHERE clause.
"""

from __future__ import annotations

import aiosqlite


async def up(db: aiosqlite.Connection) -> None:
    # Verify the table exists (fresh installs may not have it yet)
    cursor = await db.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='depth_thresholds'"
    )
    if not await cursor.fetchone():
        return

    # Update Deep floor from 172800 (48h) to 86400 (24h)
    await db.execute(
        "UPDATE depth_thresholds SET floor_seconds = 86400 "
        "WHERE depth_name = 'Deep' AND floor_seconds = 172800"
    )
