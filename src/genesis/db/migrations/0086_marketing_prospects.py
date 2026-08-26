"""Create ``marketing_prospects`` + add ``pending_outreach.labeled_surplus``.

The autonomous cold-marketing substrate needs an owner-curated, code-resolvable,
opt-out-tracked target inventory (``marketing_prospects``) — see
``db/crud/marketing_prospects.py`` for the New-Store justification.

It also threads ``labeled_surplus`` through ``pending_outreach`` so a QUEUED
marketing send (the MCP subprocess enqueue path, ``_pipeline is None``) preserves
its BULK classification when the scheduler drain rebuilds the ``OutreachRequest``.
Without this column, a queued cold send would arrive with ``labeled_surplus=False``
and mis-classify as IDENTITY instead of BULK — routing it to the wrong capability
cell and past the BULK marketing scope guard.

Idempotent (``IF NOT EXISTS`` / guarded ALTER). Fresh installs get the same DDL via
``db/schema/_tables.py``; this migration covers existing installs.
"""

from __future__ import annotations

import contextlib

import aiosqlite

_TABLE_DDL = """
    CREATE TABLE IF NOT EXISTS marketing_prospects (
        id                TEXT PRIMARY KEY,
        email             TEXT NOT NULL,
        name              TEXT,
        company           TEXT,
        status            TEXT NOT NULL DEFAULT 'active',   -- active | contacted | replied
        opted_out         INTEGER NOT NULL DEFAULT 0,       -- 1 = PERMANENT suppression (never pruned)
        source            TEXT,
        created_at        TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at        TEXT NOT NULL DEFAULT (datetime('now')),
        last_contacted_at TEXT
    )
"""

_INDEX_DDL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_marketing_prospects_email ON marketing_prospects(email COLLATE NOCASE)",
    "CREATE INDEX IF NOT EXISTS idx_marketing_prospects_active "
    "ON marketing_prospects(status, opted_out)",
)


async def _has_column(db: aiosqlite.Connection, table: str, column: str) -> bool:
    cursor = await db.execute(f"PRAGMA table_info({table})")
    return any(row[1] == column for row in await cursor.fetchall())


async def up(db: aiosqlite.Connection) -> None:
    # NOTE: must NOT call db.commit()/BEGIN — the runner owns the transaction.
    await db.execute(_TABLE_DDL)
    for stmt in _INDEX_DDL:
        await db.execute(stmt)

    # Thread labeled_surplus through the pending_outreach queue (guarded — the
    # column may already exist on a re-run or a fresh DB built from _tables.py).
    if not await _has_column(db, "pending_outreach", "labeled_surplus"):
        with contextlib.suppress(aiosqlite.OperationalError):
            await db.execute(
                "ALTER TABLE pending_outreach ADD COLUMN labeled_surplus INTEGER NOT NULL DEFAULT 0"
            )


async def down(db: aiosqlite.Connection) -> None:
    """Drop the table (development/testing only). The pending_outreach column is
    left in place — an additive column is harmless and SQLite cannot cheaply drop it."""
    await db.execute("DROP TABLE IF EXISTS marketing_prospects")
