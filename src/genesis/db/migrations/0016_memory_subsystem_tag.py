"""Add source_subsystem column for filtering automated-subsystem writes.

Phase 1.5b of the recall architecture corrections. Tags memory rows
written by automated subsystems (ego corrections, triage signals,
reflection observations) so foreground recall can default-exclude
that decisional content from user-facing queries.

Schema:
- memory_metadata.source_subsystem TEXT (NULL = user-sourced)
- pending_embeddings.source_subsystem TEXT (preserves tag through
  the embedding-recovery worker so re-embedded memories keep their
  Qdrant payload key)
- idx_memory_meta_subsystem index on memory_metadata(source_subsystem)

Backfill is reflection-only via FTS5 tag-prefix matching. Ego writes
under wing='autonomy' AND room='ego_corrections' yielded 0 prod rows
in due diligence — heuristic is unreliable, so forward-tagging only.
Triage has no clean signal in existing data. Both subsystems are
covered for new writes via the write-path plumbing.

Idempotent — column adds guarded by PRAGMA table_info; backfill
UPDATE is naturally idempotent (WHERE source_subsystem IS NULL).
"""

from __future__ import annotations

import aiosqlite


async def up(db: aiosqlite.Connection) -> None:
    # Skip cleanly on fresh DBs where the schema bootstrap (_tables.py)
    # hasn't created memory_metadata yet — the DDL schema creates the
    # column at table creation time. Mirrors the pattern in
    # 0010_bitemporal_memory.py.
    cursor = await db.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='memory_metadata'"
    )
    has_metadata = await cursor.fetchone() is not None
    cursor = await db.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='pending_embeddings'"
    )
    has_pending = await cursor.fetchone() is not None

    # 1. memory_metadata.source_subsystem
    if has_metadata:
        cursor = await db.execute("PRAGMA table_info(memory_metadata)")
        cols = {row[1] for row in await cursor.fetchall()}
        if "source_subsystem" not in cols:
            await db.execute(
                "ALTER TABLE memory_metadata ADD COLUMN source_subsystem TEXT"
            )

        # Index for default filter (WHERE source_subsystem IS NULL OR ...)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_memory_meta_subsystem "
            "ON memory_metadata(source_subsystem)"
        )

    # 2. pending_embeddings.source_subsystem (recovery-worker preservation)
    if has_pending:
        cursor = await db.execute("PRAGMA table_info(pending_embeddings)")
        pe_cols = {row[1] for row in await cursor.fetchall()}
        if "source_subsystem" not in pe_cols:
            await db.execute(
                "ALTER TABLE pending_embeddings ADD COLUMN source_subsystem TEXT"
            )

    # 3. Reflection backfill. ObservationWriter writes FTS rows with tags
    # like 'reflection_observation obs:<uuid>' and 'reflection_summary
    # obs:<uuid>'. The tag prefix is a reliable marker for subsystem-
    # originating reflection rows. Verified on prod DB: 378 rows match.
    # Skip if memory_fts doesn't exist yet (the JOIN target).
    if has_metadata:
        cursor = await db.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='memory_fts'"
        )
        if await cursor.fetchone():
            await db.execute(
                """
                UPDATE memory_metadata
                   SET source_subsystem = 'reflection'
                 WHERE source_subsystem IS NULL
                   AND memory_id IN (
                         SELECT memory_id FROM memory_fts
                          WHERE tags LIKE 'reflection_observation%'
                             OR tags LIKE 'reflection_summary%'
                   )
                """
            )


async def down(db: aiosqlite.Connection) -> None:
    # SQLite supports ALTER TABLE DROP COLUMN since 3.35 (2021).
    await db.execute(
        "DROP INDEX IF EXISTS idx_memory_meta_subsystem"
    )

    cursor = await db.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='memory_metadata'"
    )
    if await cursor.fetchone():
        cursor = await db.execute("PRAGMA table_info(memory_metadata)")
        cols = {row[1] for row in await cursor.fetchall()}
        if "source_subsystem" in cols:
            await db.execute(
                "ALTER TABLE memory_metadata DROP COLUMN source_subsystem"
            )

    cursor = await db.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='pending_embeddings'"
    )
    if await cursor.fetchone():
        cursor = await db.execute("PRAGMA table_info(pending_embeddings)")
        pe_cols = {row[1] for row in await cursor.fetchall()}
        if "source_subsystem" in pe_cols:
            await db.execute(
                "ALTER TABLE pending_embeddings DROP COLUMN source_subsystem"
            )
