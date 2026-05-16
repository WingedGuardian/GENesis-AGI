"""Add cache_read_tokens to cost_events and error_message to activity_log.

cache_read_tokens: Provider-level prompt cache hits (e.g., Anthropic,
Cerebras). Distinct from the system-level cache_hit boolean in activity_log.

error_message: Raw error string from failed LLM calls. Complements the
binary success flag for post-mortem diagnostics.
"""

from __future__ import annotations

import aiosqlite


async def up(db: aiosqlite.Connection) -> None:
    # Guard: tables may not exist on fresh DBs before schema bootstrap
    cursor = await db.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='cost_events'"
    )
    if await cursor.fetchone():
        cursor = await db.execute("PRAGMA table_info(cost_events)")
        cols = {row[1] for row in await cursor.fetchall()}
        if "cache_read_tokens" not in cols:
            await db.execute(
                "ALTER TABLE cost_events ADD COLUMN cache_read_tokens INTEGER"
            )

    cursor = await db.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='activity_log'"
    )
    if await cursor.fetchone():
        cursor = await db.execute("PRAGMA table_info(activity_log)")
        cols = {row[1] for row in await cursor.fetchall()}
        if "error_message" not in cols:
            await db.execute(
                "ALTER TABLE activity_log ADD COLUMN error_message TEXT"
            )
