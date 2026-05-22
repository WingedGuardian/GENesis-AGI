"""Add integrity tracking columns to ego_cycles and ego_proposals.

ego_cycles: output_hash, output_size for audit trail on raw ego output.
ego_proposals: content_hash for creation-time fingerprint,
              original_content to preserve pre-realist-amendment text.

Idempotent — column adds guarded by PRAGMA table_info.
"""

from __future__ import annotations

import aiosqlite


async def up(db: aiosqlite.Connection) -> None:
    # -- ego_cycles --
    cursor = await db.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='ego_cycles'"
    )
    if await cursor.fetchone():
        cursor = await db.execute("PRAGMA table_info(ego_cycles)")
        cols = {row[1] for row in await cursor.fetchall()}

        if "output_hash" not in cols:
            await db.execute(
                "ALTER TABLE ego_cycles ADD COLUMN output_hash TEXT"
            )
        if "output_size" not in cols:
            await db.execute(
                "ALTER TABLE ego_cycles ADD COLUMN output_size INTEGER"
            )

    # -- ego_proposals --
    cursor = await db.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='ego_proposals'"
    )
    if await cursor.fetchone():
        cursor = await db.execute("PRAGMA table_info(ego_proposals)")
        cols = {row[1] for row in await cursor.fetchall()}

        if "content_hash" not in cols:
            await db.execute(
                "ALTER TABLE ego_proposals ADD COLUMN content_hash TEXT"
            )
        if "original_content" not in cols:
            await db.execute(
                "ALTER TABLE ego_proposals ADD COLUMN original_content TEXT"
            )


async def down(db: aiosqlite.Connection) -> None:
    # ego_cycles
    cursor = await db.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='ego_cycles'"
    )
    if await cursor.fetchone():
        cursor = await db.execute("PRAGMA table_info(ego_cycles)")
        cols = {row[1] for row in await cursor.fetchall()}
        if "output_hash" in cols:
            await db.execute(
                "ALTER TABLE ego_cycles DROP COLUMN output_hash"
            )
        if "output_size" in cols:
            await db.execute(
                "ALTER TABLE ego_cycles DROP COLUMN output_size"
            )

    # ego_proposals
    cursor = await db.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='ego_proposals'"
    )
    if await cursor.fetchone():
        cursor = await db.execute("PRAGMA table_info(ego_proposals)")
        cols = {row[1] for row in await cursor.fetchall()}
        if "content_hash" in cols:
            await db.execute(
                "ALTER TABLE ego_proposals DROP COLUMN content_hash"
            )
        if "original_content" in cols:
            await db.execute(
                "ALTER TABLE ego_proposals DROP COLUMN original_content"
            )
