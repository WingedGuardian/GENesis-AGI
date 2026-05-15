"""Add dream cycle columns to memory_metadata + qdrant_id index on knowledge_units.

Schema changes:
- memory_metadata.deprecated INTEGER DEFAULT 0 — soft-delete flag for
  memories consolidated by the dream cycle. Independent of bi-temporal
  invalid_at (which tracks real-world fact validity).
- memory_metadata.dream_cycle_run_id TEXT — provenance tracking for
  rollback. Each dream cycle run stamps its UUID on deprecated originals.
- idx_memory_meta_deprecated on memory_metadata(deprecated) — filter
  performance for recall queries that exclude deprecated memories.
- idx_knowledge_units_qdrant_id on knowledge_units(qdrant_id) — deferred
  from PR #352; prevents full table scan on retrieved_count increment.

Idempotent — column adds guarded by PRAGMA table_info.
"""

from __future__ import annotations

import aiosqlite


async def up(db: aiosqlite.Connection) -> None:
    # Guard: table may not exist on fresh DBs before schema bootstrap
    cursor = await db.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='memory_metadata'"
    )
    has_metadata = await cursor.fetchone() is not None

    if has_metadata:
        cursor = await db.execute("PRAGMA table_info(memory_metadata)")
        cols = {row[1] for row in await cursor.fetchall()}

        if "deprecated" not in cols:
            await db.execute(
                "ALTER TABLE memory_metadata "
                "ADD COLUMN deprecated INTEGER NOT NULL DEFAULT 0"
            )
        if "dream_cycle_run_id" not in cols:
            await db.execute(
                "ALTER TABLE memory_metadata "
                "ADD COLUMN dream_cycle_run_id TEXT"
            )

        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_memory_meta_deprecated "
            "ON memory_metadata(deprecated)"
        )

    # Bonus: index on knowledge_units.qdrant_id (deferred from PR #352).
    # Without this, increment_retrieved_batch() does a full table scan.
    cursor = await db.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='knowledge_units'"
    )
    if await cursor.fetchone():
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_knowledge_units_qdrant_id "
            "ON knowledge_units(qdrant_id)"
        )


async def down(db: aiosqlite.Connection) -> None:
    await db.execute("DROP INDEX IF EXISTS idx_memory_meta_deprecated")
    await db.execute("DROP INDEX IF EXISTS idx_knowledge_units_qdrant_id")

    cursor = await db.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='memory_metadata'"
    )
    if await cursor.fetchone():
        cursor = await db.execute("PRAGMA table_info(memory_metadata)")
        cols = {row[1] for row in await cursor.fetchall()}
        if "deprecated" in cols:
            await db.execute(
                "ALTER TABLE memory_metadata DROP COLUMN deprecated"
            )
        if "dream_cycle_run_id" in cols:
            await db.execute(
                "ALTER TABLE memory_metadata DROP COLUMN dream_cycle_run_id"
            )
