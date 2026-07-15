"""Ledger table for the data-migration framework (WS-C).

Schema migrations (this directory) alter STRUCTURE and run once, atomically,
blocking boot. DATA migrations (``db/data_migrations/``) backfill/transform
non-schema state (Qdrant payloads, entity graphs) — long-running, idempotent,
run post-boot in the background, and must never abort boot. This table is
their once-per-install ledger; the runner claims a row atomically before
running so the server + bridge-fallback cannot double-dispatch one migration.

status: pending -> running -> completed | failed (retryable next boot);
operator_pending is a migration declared ``requires_operator = True`` that the
auto-runner never claims (it waits for a deliberate operator trigger).
"""

from __future__ import annotations

import aiosqlite


async def up(db: aiosqlite.Connection) -> None:
    # IF NOT EXISTS: init_db() runs create_all_tables() (which creates this
    # table from schema/_tables.py) BEFORE the migration runner, so on a fresh
    # install the table already exists when 0060 runs. A bare CREATE would raise
    # "table already exists" and abort boot. Same pattern as 0059.
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS data_migrations (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            status TEXT NOT NULL CHECK (
                status IN ('pending', 'running', 'completed', 'failed', 'operator_pending')
            ),
            attempts INTEGER NOT NULL DEFAULT 0,
            started_at TEXT,
            completed_at TEXT,
            error TEXT,
            summary TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )


async def down(db: aiosqlite.Connection) -> None:
    await db.execute("DROP TABLE IF EXISTS data_migrations")
