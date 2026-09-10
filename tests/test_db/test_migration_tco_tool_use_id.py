"""Migration ``20260904180225_tco_tool_use_id``: tool_use_id + UNIQUE index (#1597 dedup).

The module is addressed by its FULL timestamp id on purpose. It was
``0092_tco_tool_use_id`` until the legacy-numeric freeze (#1678) forced the
rename, and this import kept the dead name — so collection of the whole
``tests/`` tree died on ``ModuleNotFoundError`` (Codex P1, PR #1616). An
explicit name is still right for a test: it fails loudly at collection rather
than silently binding whatever migration a glob happened to find first.
"""

from __future__ import annotations

import importlib

import aiosqlite
import pytest

migration = importlib.import_module("genesis.db.migrations.20260904180225_tco_tool_use_id")


async def _legacy_db() -> aiosqlite.Connection:
    """Pre-migration tool_call_outcomes shape (no tool_use_id column/index)."""
    db = await aiosqlite.connect(":memory:")
    await db.execute(
        """CREATE TABLE tool_call_outcomes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            tool_name TEXT NOT NULL,
            file_path TEXT,
            success INTEGER NOT NULL DEFAULT 1,
            error_snippet TEXT,
            timestamp TEXT NOT NULL
        )"""
    )
    await db.commit()
    return db


async def _cols(db: aiosqlite.Connection, table: str) -> set[str]:
    cur = await db.execute(f"PRAGMA table_info({table})")  # noqa: S608
    return {row[1] for row in await cur.fetchall()}


async def _indexes(db: aiosqlite.Connection, table: str) -> set[str]:
    cur = await db.execute(f"PRAGMA index_list({table})")  # noqa: S608
    return {row[1] for row in await cur.fetchall()}


async def _insert(db, *, tool_name="Edit", success=1, tool_use_id=None):
    await db.execute(
        "INSERT OR IGNORE INTO tool_call_outcomes "
        "(tool_name, success, timestamp, tool_use_id) VALUES (?, ?, '2026-09-02', ?)",
        (tool_name, success, tool_use_id),
    )


@pytest.mark.asyncio
async def test_up_adds_column_and_index():
    db = await _legacy_db()
    try:
        await migration.up(db)
        assert "tool_use_id" in await _cols(db, "tool_call_outcomes")
        assert "idx_tco_tool_use_id" in await _indexes(db, "tool_call_outcomes")
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_up_is_idempotent():
    db = await _legacy_db()
    try:
        await migration.up(db)
        await migration.up(db)  # duplicate ADD/CREATE must not raise
        assert "tool_use_id" in await _cols(db, "tool_call_outcomes")
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_existing_rows_read_null():
    db = await _legacy_db()
    try:
        await db.execute(
            "INSERT INTO tool_call_outcomes (tool_name, success, timestamp) "
            "VALUES ('Edit', 1, '2026-01-01')"
        )
        await migration.up(db)
        cur = await db.execute("SELECT tool_use_id FROM tool_call_outcomes")
        assert (await cur.fetchone())[0] is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_multiple_null_tool_use_ids_allowed():
    """Load-bearing SQLite semantics: a UNIQUE index treats each NULL as distinct,
    so the 25k existing rows + all future success rows (NULL tool_use_id) insert
    freely. If this ever regressed, the success path would start failing."""
    db = await _legacy_db()
    try:
        await migration.up(db)
        await _insert(db, tool_use_id=None)
        await _insert(db, tool_use_id=None)
        await db.commit()
        cur = await db.execute("SELECT COUNT(*) FROM tool_call_outcomes")
        assert (await cur.fetchone())[0] == 2
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_duplicate_tool_use_id_ignored():
    """INSERT OR IGNORE on a repeated tool_use_id is a no-op — the dedup that makes
    per-turn Stop rescans idempotent."""
    db = await _legacy_db()
    try:
        await migration.up(db)
        await _insert(db, success=0, tool_use_id="toolu_dup")
        await _insert(db, success=0, tool_use_id="toolu_dup")
        await db.commit()
        cur = await db.execute("SELECT COUNT(*) FROM tool_call_outcomes")
        assert (await cur.fetchone())[0] == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_up_on_a_bare_database_is_a_no_op():
    """A database WITHOUT tool_call_outcomes must not take the numbered run down.

    ``CREATE UNIQUE INDEX IF NOT EXISTS`` guards the INDEX, never the TABLE, so
    without the table-existence check SQLite raises ``no such table`` and
    ``MigrationRunner.run_pending()`` aborts every later migration on every
    install (Codex P2, PR #1616). The runner suite applies migrations to a bare
    database by design, so this is the shape it actually hits.
    """
    db = await aiosqlite.connect(":memory:")
    try:
        await migration.up(db)  # must not raise
        # `down()` is called too, but note what that does and does not prove:
        # removing its own table check leaves this GREEN, because
        # `PRAGMA table_info(<missing>)` returns zero rows instead of raising, so
        # the ALTER is skipped anyway. Mutation-verified. Kept as a smoke check on
        # the reverse path, NOT claimed as coverage of that guard.
        await migration.down(db)
        cur = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='tool_call_outcomes'"
        )
        assert await cur.fetchone() is None, "up() must not invent the table it only extends"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_down_drops_column_and_index():
    db = await _legacy_db()
    try:
        await migration.up(db)
        await migration.down(db)
        assert "tool_use_id" not in await _cols(db, "tool_call_outcomes")
        assert "idx_tco_tool_use_id" not in await _indexes(db, "tool_call_outcomes")
    finally:
        await db.close()
