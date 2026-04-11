"""Add update_history table for tracking applied Genesis updates.

Records each update attempt with before/after versions, success/failure
status, and rollback information. Used by the update infrastructure to
track update history and diagnose failures.
"""

from __future__ import annotations

import aiosqlite


async def up(db: aiosqlite.Connection) -> None:
    await db.execute("""
        CREATE TABLE IF NOT EXISTS update_history (
            id TEXT PRIMARY KEY,
            old_tag TEXT NOT NULL,
            new_tag TEXT NOT NULL,
            old_commit TEXT NOT NULL,
            new_commit TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('success', 'failed', 'rolled_back')),
            rollback_tag TEXT,
            failure_reason TEXT,
            degraded_subsystems TEXT,
            started_at TEXT NOT NULL,
            completed_at TEXT NOT NULL
        )
    """)


async def down(db: aiosqlite.Connection) -> None:
    await db.execute("DROP TABLE IF EXISTS update_history")
