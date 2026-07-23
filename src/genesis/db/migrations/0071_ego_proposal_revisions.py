"""Add the revision/revalidation dark schema for the ego proposal-lifecycle
redesign (PR-4): three columns on ego_proposals plus the ego_proposal_revisions
audit table.

- ego_proposals gains revision_num / revalidate_at / last_validated_at.
- ego_proposal_revisions stores prior (content, rationale, confidence,
  execution_plan, expected_outputs) snapshots so the PR-5 reconcile stage can
  revise a pending proposal in place without laundering lineage. Surrogate id
  PK, no FK (mirrors calibration_cell_history; SQLite FK enforcement is
  PRAGMA-gated, so proposal_id is a documented soft reference).

Dark in PR-4 — no writer until PR-5.

Self-contained upgrade path: the column ADDs are PRAGMA-guarded so this migration
fully applies its own schema whether it runs via create_all_tables' base path
(where _migrate_add_columns has already added the columns — the guards then skip)
OR via the standalone numbered-migration runner (python -m genesis.db.migrations
--apply, used by update.sh) with no create_all_tables. Guarding is what makes the
double path safe: an unguarded ADD COLUMN after _migrate_add_columns already ran
would raise 'duplicate column' and hard-fail the runner. Fresh/test DBs get
everything from the canonical CREATE TABLE in db/schema/_tables.py. No commit —
the runner owns the transaction.
"""

from __future__ import annotations

import aiosqlite

_EGO_COLUMNS = (
    ("revision_num", "ALTER TABLE ego_proposals ADD COLUMN revision_num INTEGER DEFAULT 1"),
    ("revalidate_at", "ALTER TABLE ego_proposals ADD COLUMN revalidate_at TEXT"),
    ("last_validated_at", "ALTER TABLE ego_proposals ADD COLUMN last_validated_at TEXT"),
)


async def up(db: aiosqlite.Connection) -> None:
    # Guarded column adds — idempotent and safe regardless of whether
    # _migrate_add_columns has already run on this DB.
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='ego_proposals'"
    )
    if await cursor.fetchone():
        cursor = await db.execute("PRAGMA table_info(ego_proposals)")
        cols = {row[1] for row in await cursor.fetchall()}
        for name, ddl in _EGO_COLUMNS:
            if name not in cols:
                await db.execute(ddl)

    await db.execute(
        """CREATE TABLE IF NOT EXISTS ego_proposal_revisions (
            id               TEXT PRIMARY KEY,
            proposal_id      TEXT NOT NULL,
            revision_num     INTEGER NOT NULL,
            content          TEXT,
            rationale        TEXT,
            confidence       REAL,
            execution_plan   TEXT,
            expected_outputs TEXT,
            revised_at       TEXT NOT NULL,
            revised_by       TEXT,
            reason           TEXT
        )"""
    )


async def down(db: aiosqlite.Connection) -> None:
    await db.execute("DROP TABLE IF EXISTS ego_proposal_revisions")
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='ego_proposals'"
    )
    if await cursor.fetchone():
        cursor = await db.execute("PRAGMA table_info(ego_proposals)")
        cols = {row[1] for row in await cursor.fetchall()}
        for name, _ddl in _EGO_COLUMNS:
            if name in cols:
                await db.execute(f"ALTER TABLE ego_proposals DROP COLUMN {name}")
