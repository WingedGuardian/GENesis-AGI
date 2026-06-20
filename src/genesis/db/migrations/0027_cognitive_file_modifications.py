"""Create ``cognitive_file_modifications`` — the cognitive self-modification ledger.

Genesis autonomously OVERWRITES several of its own cognitive config files at
runtime (skill ``SKILL.md`` refinement, daily ``TRIAGE_CALIBRATION.md`` regen,
daily ``USER_KNOWLEDGE.md`` synthesis) with NO backup and NO undo. If a self-edit
degrades cognition there is currently no way to revert it. This table is an
append-only ledger that captures the PRE-IMAGE (``prior_content``) before each such
overwrite, so a bad self-edit can be rolled back.

File overwrites are NOT invertible (unlike the dream-cycle's DB-op tagging), so the
pre-image must be stored inline. Files are small (<8 KB) → inline storage is
queryable, transactional, and captured by the 6-hourly ``backup.sh`` DB dump.

NOT to be confused with the (dead, CC-tool-audit) ``file_modifications`` table —
different scope (Genesis cognitive self-edits), and it stores pre/post images.

This table is an OPERATOR SURFACE ONLY: no automated cognitive path reads it (same
discipline as ``ego_calibration_snapshots``). Rollback is manual/programmatic in v1.

Idempotent (``IF NOT EXISTS``). Fresh installs get the same DDL via ``_tables.py``.
"""

from __future__ import annotations

import aiosqlite

_TABLE_DDL = """
    CREATE TABLE IF NOT EXISTS cognitive_file_modifications (
        id              TEXT PRIMARY KEY,
        actor           TEXT NOT NULL,
        target_path     TEXT NOT NULL,
        prior_content   TEXT,
        applied_content TEXT NOT NULL,
        change_summary  TEXT,
        metadata        TEXT,
        status          TEXT NOT NULL DEFAULT 'applied',
        created_at      TEXT NOT NULL DEFAULT (datetime('now')),
        rolled_back_at  TEXT
    )
"""

_INDEX_DDL = [
    "CREATE INDEX IF NOT EXISTS idx_cog_file_mods_target "
    "ON cognitive_file_modifications(target_path)",
    "CREATE INDEX IF NOT EXISTS idx_cog_file_mods_actor "
    "ON cognitive_file_modifications(actor)",
    "CREATE INDEX IF NOT EXISTS idx_cog_file_mods_created "
    "ON cognitive_file_modifications(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_cog_file_mods_status "
    "ON cognitive_file_modifications(status)",
]


async def up(db: aiosqlite.Connection) -> None:
    # NOTE: must NOT call db.commit()/BEGIN — the runner owns the transaction.
    await db.execute(_TABLE_DDL)
    for stmt in _INDEX_DDL:
        await db.execute(stmt)


async def down(db: aiosqlite.Connection) -> None:
    """Drop the table (development/testing only)."""
    await db.execute("DROP TABLE IF EXISTS cognitive_file_modifications")
