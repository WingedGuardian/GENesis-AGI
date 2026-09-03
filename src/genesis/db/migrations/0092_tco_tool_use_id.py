"""Add ``tool_call_outcomes.tool_use_id`` — dedup key for the failure scanner.

Failed Edit/Write calls fire no PostToolUse/PostToolUseFailure hook on current
Claude Code (measured 2026-09-02, CC 2.1.246 — issue #1597), so the sensor never
recorded a single failure (0 / 25k rows). The fix scans the session transcript on
the Stop event and records EVERY Edit/Write outcome (``success`` 0 or 1); Stop
re-fires every turn over a growing transcript, so the recorder is made idempotent
by a UNIQUE ``tool_use_id`` and ``INSERT OR IGNORE``. ``tool_use_id`` (``toolu_…``)
is globally unique per CC.

Nullable, NO default: every row the scanner writes carries a value; the pre-#1597
rows carry NULL. SQLite treats each NULL in a UNIQUE index as distinct, so the ~25k
grandfathered NULL rows coexist freely.

Self-contained + idempotent: the ADD is PRAGMA-guarded and the index uses
IF NOT EXISTS, so this applies cleanly whether the column/index arrived first via
the base create_all_tables path (``_tables.py`` CREATE + INDEXES +
``_migrations.py::_migrate_add_columns``) or via this standalone numbered runner.
The column is ALSO in ``_migrate_add_columns`` — required, not redundant: INDEXES is
built before the numbered runner, so the unique index would crash a legacy-DB
bootstrap if the column were added only here (the #1123/#1127 class;
tests/test_db/test_schema_base_path_parity.py guards it).

(Numbered 0092: 0091_topic_recency_stamps is the highest present on disk. The runner
applies by per-id tracking; only duplicate prefixes are fatal.)
"""

from __future__ import annotations

import aiosqlite


async def _add_column(db: aiosqlite.Connection, table: str, col: str, decl: str) -> None:
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    )
    if not await cursor.fetchone():
        return  # fresh DB — CREATE TABLE / base-path ALTER already carries the column
    cursor = await db.execute(f"PRAGMA table_info({table})")
    cols = {row[1] for row in await cursor.fetchall()}
    if col not in cols:
        await db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


async def up(db: aiosqlite.Connection) -> None:
    await _add_column(db, "tool_call_outcomes", "tool_use_id", "TEXT")
    await db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_tco_tool_use_id ON tool_call_outcomes(tool_use_id)"
    )


async def down(db: aiosqlite.Connection) -> None:
    # Dev/testing only — both steps guarded.
    await db.execute("DROP INDEX IF EXISTS idx_tco_tool_use_id")
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='tool_call_outcomes'"
    )
    if not await cursor.fetchone():
        return
    cursor = await db.execute("PRAGMA table_info(tool_call_outcomes)")
    cols = {row[1] for row in await cursor.fetchall()}
    if "tool_use_id" in cols:
        await db.execute("ALTER TABLE tool_call_outcomes DROP COLUMN tool_use_id")
