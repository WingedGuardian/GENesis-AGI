"""Add goal_id to ego_proposals for goal-proposal linking.

Enables the ego to tag proposals with the user goal they advance.
Nullable — not all proposals serve a specific goal.

Idempotent — column add guarded by PRAGMA table_info.
"""

from __future__ import annotations

import aiosqlite


async def up(db: aiosqlite.Connection) -> None:
    cursor = await db.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='ego_proposals'"
    )
    if not await cursor.fetchone():
        return  # Table doesn't exist yet (fresh DB, schema bootstrap handles it)

    cursor = await db.execute("PRAGMA table_info(ego_proposals)")
    cols = {row[1] for row in await cursor.fetchall()}

    if "goal_id" not in cols:
        await db.execute(
            "ALTER TABLE ego_proposals ADD COLUMN goal_id TEXT"
        )

    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_ego_proposals_goal "
        "ON ego_proposals(goal_id)"
    )


async def down(db: aiosqlite.Connection) -> None:
    await db.execute("DROP INDEX IF EXISTS idx_ego_proposals_goal")

    cursor = await db.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='ego_proposals'"
    )
    if await cursor.fetchone():
        cursor = await db.execute("PRAGMA table_info(ego_proposals)")
        cols = {row[1] for row in await cursor.fetchall()}
        if "goal_id" in cols:
            await db.execute(
                "ALTER TABLE ego_proposals DROP COLUMN goal_id"
            )
