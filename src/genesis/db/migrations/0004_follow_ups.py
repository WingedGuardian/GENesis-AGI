"""Add follow_ups table — accountability ledger for deferred work.

Tracks follow-up items from ego cycles, foreground sessions, surplus
scheduler, and recon pipeline through to completion or escalation.
"""

from __future__ import annotations

import aiosqlite


async def up(db: aiosqlite.Connection) -> None:
    await db.execute("""
        CREATE TABLE IF NOT EXISTS follow_ups (
            id               TEXT PRIMARY KEY,
            source           TEXT NOT NULL,
            source_session   TEXT,
            content          TEXT NOT NULL,
            reason           TEXT,
            strategy         TEXT NOT NULL CHECK (
                strategy IN ('scheduled_task', 'surplus_task', 'ego_judgment', 'user_input_needed')
            ),
            scheduled_at     TEXT,
            status           TEXT NOT NULL DEFAULT 'pending' CHECK (
                status IN ('pending', 'scheduled', 'in_progress', 'completed', 'failed', 'blocked')
            ),
            linked_task_id   TEXT,
            priority         TEXT NOT NULL DEFAULT 'medium' CHECK (
                priority IN ('low', 'medium', 'high', 'critical')
            ),
            created_at       TEXT NOT NULL,
            completed_at     TEXT,
            resolution_notes TEXT,
            blocked_reason   TEXT,
            escalated_to     TEXT
        )
    """)
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_follow_ups_status ON follow_ups(status)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_follow_ups_scheduled ON follow_ups(scheduled_at)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_follow_ups_source ON follow_ups(source)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_follow_ups_linked_task ON follow_ups(linked_task_id)"
    )


async def down(db: aiosqlite.Connection) -> None:
    await db.execute("DROP TABLE IF EXISTS follow_ups")
