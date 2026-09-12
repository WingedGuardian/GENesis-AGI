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


async def _has_table(db: aiosqlite.Connection, table: str) -> bool:
    cursor = await db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,))
    return await cursor.fetchone() is not None


async def _has_column(db: aiosqlite.Connection, table: str, column: str) -> bool:
    cursor = await db.execute(f"PRAGMA table_info({table})")
    return any(row[1] == column for row in await cursor.fetchall())


async def up(db: aiosqlite.Connection) -> None:
    # NOTE: must NOT call db.commit()/BEGIN — the runner owns the transaction.
    await db.execute(_TABLE_DDL)
    for stmt in _INDEX_DDL:
        await db.execute(stmt)

    # Thread labeled_surplus through the pending_outreach queue — but ONLY when that
    # table already exists. pending_outreach is created by create_all_tables
    # (db/schema/_tables.py), NOT by any migration, and in production create_all_tables
    # runs BEFORE the migration runner — so this ALTER only ever fires on a legacy DB
    # whose pending_outreach predates labeled_surplus. When the runner is exercised in
    # ISOLATION (no create_all_tables — e.g. the migration-runner test harness), the
    # table is legitimately absent and there is nothing to thread: skip rather than
    # fail on "no such table". The _has_column guard gives idempotency (fresh installs
    # already carry the column). Crucially this is NOT a blanket error suppress — a
    # real ALTER failure still PROPAGATES: a transient SQLITE_LOCKED reaches the
    # runner's retry-on-lock loop (a swallowed lock would instead record 0089 as
    # applied WITHOUT the column, breaking every subsequent pending_outreach enqueue),
    # and a genuine schema failure fails the migration loudly.
    if await _has_table(db, "pending_outreach") and not await _has_column(
        db, "pending_outreach", "labeled_surplus"
    ):
        await db.execute(
            "ALTER TABLE pending_outreach ADD COLUMN labeled_surplus INTEGER NOT NULL DEFAULT 0"
        )


async def down(db: aiosqlite.Connection) -> None:
    """Drop the table (development/testing only). The pending_outreach column is
    left in place — an additive column is harmless and SQLite cannot cheaply drop it."""
    await db.execute("DROP TABLE IF EXISTS marketing_prospects")
