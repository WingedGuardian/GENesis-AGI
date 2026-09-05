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

(UTC-TIMESTAMP id, not a hand-allocated number. The legacy numeric namespace is
FROZEN to its enumerated set — `scripts/check_migration_prefixes.py` refuses a new
`00NN` file, because hand-allocation is what let two branches claim one id. This
file was `0092_tco_tool_use_id.py` and was renamed when that freeze landed on
main; the runner tracks applied migrations by id, so the rename means this
migration is applied under its new id. That is correct here and only here: it has
never been applied anywhere, having lived on an unmerged branch throughout.)
"""

from __future__ import annotations

import aiosqlite


async def _table_exists(db: aiosqlite.Connection, table: str) -> bool:
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    )
    return await cursor.fetchone() is not None


async def _add_column(db: aiosqlite.Connection, table: str, col: str, decl: str) -> None:
    cursor = await db.execute(f"PRAGMA table_info({table})")
    cols = {row[1] for row in await cursor.fetchall()}
    if col not in cols:
        await db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


async def up(db: aiosqlite.Connection) -> None:
    # THE TABLE-EXISTENCE CHECK GOVERNS BOTH STATEMENTS, not just the ALTER.
    # `IF NOT EXISTS` on a CREATE INDEX guards against the INDEX already being
    # there; it does nothing about a missing TABLE, and SQLite raises
    # `no such table` — so on any database that does not carry
    # `tool_call_outcomes` this migration failed outright and took the whole
    # numbered run down with it. That is not hypothetical: the migrations-runner
    # suite applies migrations against a bare database by design, and it went
    # red (Codex P2, PR #1616).
    #
    # Skipping is the correct answer rather than creating the table here: this
    # migration's job is to ADD a column and an index to a table the base schema
    # path owns. A database without that table has never had the base path run,
    # and inventing a table shape in a numbered migration is how two definitions
    # of one table start to drift.
    if not await _table_exists(db, "tool_call_outcomes"):
        return  # base schema path has not created it — nothing to add it to
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
