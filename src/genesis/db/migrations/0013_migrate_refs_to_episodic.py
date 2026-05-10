"""Migrate reference entries from knowledge_base to episodic_memory.

References (credentials, URLs, IPs, etc.) are personal data that should live
alongside other episodic memories, not in the external-knowledge collection.
This updates the collection label in memory_metadata and memory_fts to match
the Qdrant migration performed at init time.

Idempotent — safe to run multiple times. Rows already set to 'episodic_memory'
are unaffected (UPDATE with WHERE filters them out).
"""

from __future__ import annotations

import aiosqlite


async def up(db: aiosqlite.Connection) -> None:
    # Check if knowledge_units table exists (fresh installs may not have it yet)
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='knowledge_units'"
    )
    if not await cursor.fetchone():
        return

    # Update memory_metadata: set collection to episodic_memory for reference entries
    await db.execute(
        """
        UPDATE memory_metadata SET collection = 'episodic_memory'
        WHERE collection = 'knowledge_base'
          AND memory_id IN (
              SELECT qdrant_id FROM knowledge_units
              WHERE project_type = 'reference' AND qdrant_id IS NOT NULL
          )
        """
    )

    # Update memory_fts: same collection label change
    await db.execute(
        """
        UPDATE memory_fts SET collection = 'episodic_memory'
        WHERE collection = 'knowledge_base'
          AND memory_id IN (
              SELECT qdrant_id FROM knowledge_units
              WHERE project_type = 'reference' AND qdrant_id IS NOT NULL
          )
        """
    )
