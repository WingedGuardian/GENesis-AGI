"""Fix ego_proposals CHECK constraint — add 'tabled' and 'withdrawn' statuses.

Migration 0007 was supposed to add these values but was short-circuited by the
_migrate_add_columns() function which added the 'rank' column first via ALTER
TABLE, causing 0007's idempotency check (``if 'rank' in cols: return``) to skip
the table rebuild.  The CHECK constraint on status never got updated.

SQLite cannot ALTER CHECK constraints — requires full table rebuild.
"""

from __future__ import annotations

import aiosqlite


async def up(db: aiosqlite.Connection) -> None:
    # Idempotency: check if the fix is already applied
    cursor = await db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='ego_proposals'"
    )
    row = await cursor.fetchone()
    if not row:
        return  # Table doesn't exist (fresh install creates it correctly)

    ddl = row[0] or ""
    if "'tabled'" in ddl and "'withdrawn'" in ddl:
        return  # Already has the expanded constraint

    # Clean up orphaned temp table from a prior failed attempt
    await db.execute("DROP TABLE IF EXISTS ego_proposals_new")

    await db.execute("""
        CREATE TABLE ego_proposals_new (
            id              TEXT PRIMARY KEY,
            action_type     TEXT NOT NULL,
            action_category TEXT NOT NULL DEFAULT '',
            content         TEXT NOT NULL,
            rationale       TEXT NOT NULL DEFAULT '',
            confidence      REAL NOT NULL DEFAULT 0.0,
            urgency         TEXT NOT NULL DEFAULT 'normal'
                CHECK (urgency IN ('low', 'normal', 'high', 'critical')),
            alternatives    TEXT NOT NULL DEFAULT '',
            status          TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'approved', 'rejected',
                                  'expired', 'executed', 'failed',
                                  'tabled', 'withdrawn')),
            user_response   TEXT,
            cycle_id        TEXT,
            batch_id        TEXT,
            created_at      TEXT NOT NULL,
            resolved_at     TEXT,
            expires_at      TEXT,
            rank            INTEGER,
            execution_plan  TEXT,
            recurring       INTEGER DEFAULT 0,
            memory_basis    TEXT DEFAULT ''
        )
    """)

    await db.execute("""
        INSERT INTO ego_proposals_new
            (id, action_type, action_category, content, rationale,
             confidence, urgency, alternatives, status, user_response,
             cycle_id, batch_id, created_at, resolved_at, expires_at,
             rank, execution_plan, recurring, memory_basis)
        SELECT
            id, action_type, action_category, content, rationale,
            confidence, urgency, alternatives, status, user_response,
            cycle_id, batch_id, created_at, resolved_at, expires_at,
            rank, execution_plan, recurring, memory_basis
        FROM ego_proposals
    """)

    await db.execute("DROP TABLE ego_proposals")
    await db.execute("ALTER TABLE ego_proposals_new RENAME TO ego_proposals")

    # Recreate all indexes
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_ego_proposals_status "
        "ON ego_proposals(status)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_ego_proposals_created "
        "ON ego_proposals(created_at)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_ego_proposals_cycle "
        "ON ego_proposals(cycle_id)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_ego_proposals_category "
        "ON ego_proposals(action_category, status)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_ego_proposals_batch "
        "ON ego_proposals(batch_id)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_ego_proposals_expires "
        "ON ego_proposals(expires_at)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_ego_proposals_rank "
        "ON ego_proposals(status, rank)"
    )
