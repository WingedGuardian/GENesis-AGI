"""Add bi-temporal columns to memory_metadata.

Enables temporal fact tracking inspired by Graphiti's bi-temporal model:
- valid_at: when the fact became true in the real world
- invalid_at: when the fact stopped being true (NULL = still valid)

This allows queries like:
- "What was true about X at time T?" (WHERE valid_at <= T AND (invalid_at IS NULL OR invalid_at > T))
- "What facts have been superseded?" (WHERE invalid_at IS NOT NULL)
- "Show me the history of changes to X" (ORDER BY valid_at)

The existing created_at column tracks when the memory entered the system
(system time). valid_at/invalid_at track when the fact was true in the
world (world time). This separation is the core of bi-temporal modeling.
"""

from __future__ import annotations

import aiosqlite


async def up(db: aiosqlite.Connection) -> None:
    # Add bi-temporal columns (idempotent — column may already exist from DDL)
    cursor = await db.execute("PRAGMA table_info(memory_metadata)")
    existing = {row[1] for row in await cursor.fetchall()}

    if "valid_at" not in existing:
        await db.execute("""
            ALTER TABLE memory_metadata
            ADD COLUMN valid_at TEXT
        """)
    if "invalid_at" not in existing:
        await db.execute("""
            ALTER TABLE memory_metadata
            ADD COLUMN invalid_at TEXT
        """)

    # Index for temporal queries (find facts valid at a point in time)
    await db.execute("""
        CREATE INDEX IF NOT EXISTS idx_memory_meta_valid_at
        ON memory_metadata(valid_at)
    """)
    await db.execute("""
        CREATE INDEX IF NOT EXISTS idx_memory_meta_invalid_at
        ON memory_metadata(invalid_at)
    """)

    # Backfill: set valid_at = created_at for existing memories
    # (best approximation — fact became true when we learned it)
    await db.execute("""
        UPDATE memory_metadata
        SET valid_at = created_at
        WHERE valid_at IS NULL
    """)


async def down(db: aiosqlite.Connection) -> None:
    # SQLite doesn't support DROP COLUMN before 3.35.0
    # For safety, just drop the indexes
    await db.execute("DROP INDEX IF EXISTS idx_memory_meta_valid_at")
    await db.execute("DROP INDEX IF EXISTS idx_memory_meta_invalid_at")
