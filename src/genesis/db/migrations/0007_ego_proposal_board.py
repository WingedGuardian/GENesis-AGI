"""Ego proposal board — add tabled/withdrawn status, rank, execution_plan, recurring.

The SQLite CHECK constraint on ego_proposals.status restricts values to a
fixed set. Adding 'tabled' and 'withdrawn' requires a full table recreation
(ALTER TABLE cannot modify CHECK constraints). Also adds three new columns:
rank (board position), execution_plan (dispatch instructions), recurring flag.
"""

from __future__ import annotations

import aiosqlite


async def up(db: aiosqlite.Connection) -> None:
    # Only run if the table exists
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='ego_proposals'"
    )
    if not await cursor.fetchone():
        return

    # Check if migration already applied (rank column exists)
    col_cursor = await db.execute("PRAGMA table_info(ego_proposals)")
    cols = {row[1] for row in await col_cursor.fetchall()}
    if "rank" in cols:
        return  # already migrated

    # Recreate table with expanded CHECK and new columns
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
            recurring       INTEGER DEFAULT 0
        )
    """)

    # Copy existing data (new columns get defaults: rank=NULL,
    # execution_plan=NULL, recurring=0)
    await db.execute("""
        INSERT INTO ego_proposals_new
            (id, action_type, action_category, content, rationale,
             confidence, urgency, alternatives, status, user_response,
             cycle_id, batch_id, created_at, resolved_at, expires_at)
        SELECT
            id, action_type, action_category, content, rationale,
            confidence, urgency, alternatives, status, user_response,
            cycle_id, batch_id, created_at, resolved_at, expires_at
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
    # New index for board queries (pending ordered by rank)
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_ego_proposals_rank "
        "ON ego_proposals(status, rank)"
    )
