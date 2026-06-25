"""Migration 0037 — rename ``procedural_memory.speculative`` -> ``draft``.

A procedure's unproven state is not "speculative" (the word Genesis reserves
for observations and hypothesis-claims); it is an untested *draft*. This
migration renames ONLY the procedural_memory column. ``observations.speculative``
and ``speculative_claims.speculative`` are a different concept and stay.

The test builds the *pre-migration* schema explicitly (a raw table carrying a
``speculative`` column) so it exercises the real ``ALTER TABLE … RENAME COLUMN``
regardless of the current ``_tables.py`` DDL — a migration must keep working on
databases born under the old schema forever.
"""

from __future__ import annotations

import importlib

import aiosqlite
import pytest

M37 = importlib.import_module(
    "genesis.db.migrations.0037_rename_procedure_speculative_to_draft"
)


async def _columns(db: aiosqlite.Connection, table: str) -> set[str]:
    cur = await db.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in await cur.fetchall()}


async def _indexes(db: aiosqlite.Connection) -> set[str]:
    cur = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='index'"
    )
    return {row[0] for row in await cur.fetchall()}


async def _build_pre_state(conn: aiosqlite.Connection) -> None:
    """Construct the schema as it existed *before* 0037: procedural_memory
    with a ``speculative`` column + its index, plus the two sibling tables
    that must be left untouched."""
    await conn.execute(
        """
        CREATE TABLE procedural_memory (
            id           TEXT PRIMARY KEY,
            task_type    TEXT NOT NULL,
            speculative  INTEGER NOT NULL DEFAULT 1,
            confidence   REAL NOT NULL DEFAULT 0.0,
            deprecated   INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    await conn.execute(
        "CREATE INDEX idx_procedural_speculative "
        "ON procedural_memory(speculative)"
    )
    # Sibling tables that legitimately keep their own ``speculative`` column.
    await conn.execute(
        "CREATE TABLE observations (id TEXT PRIMARY KEY, "
        "speculative INTEGER NOT NULL DEFAULT 0)"
    )
    await conn.execute(
        "CREATE TABLE speculative_claims (id TEXT PRIMARY KEY, "
        "speculative INTEGER NOT NULL DEFAULT 1)"
    )
    await conn.commit()


@pytest.fixture
async def db(tmp_path):
    async with aiosqlite.connect(str(tmp_path / "t.db")) as conn:
        conn.row_factory = aiosqlite.Row
        await _build_pre_state(conn)
        yield conn


@pytest.mark.asyncio
async def test_renames_column_preserving_values(db):
    await db.execute(
        "INSERT INTO procedural_memory (id, task_type, speculative, confidence) "
        "VALUES ('draftp', 'td', 1, 0.5), ('validp', 'tv', 0, 0.9)"
    )
    await db.commit()

    await M37.up(db)

    cols = await _columns(db, "procedural_memory")
    assert "draft" in cols
    assert "speculative" not in cols

    # Values survive the rename, mapped onto the new column name.
    cur = await db.execute(
        "SELECT draft, confidence FROM procedural_memory ORDER BY id"
    )
    rows = await cur.fetchall()
    assert (rows[0]["draft"], rows[0]["confidence"]) == (1, 0.5)   # draftp
    assert (rows[1]["draft"], rows[1]["confidence"]) == (0, 0.9)   # validp


@pytest.mark.asyncio
async def test_index_renamed(db):
    await M37.up(db)
    idx = await _indexes(db)
    assert "idx_procedural_speculative" not in idx
    assert "idx_procedural_draft" in idx
    # The new index actually covers the new column.
    cur = await db.execute("PRAGMA index_info(idx_procedural_draft)")
    covered = {row[2] for row in await cur.fetchall()}
    assert covered == {"draft"}


@pytest.mark.asyncio
async def test_leaves_observations_and_claims_untouched(db):
    await M37.up(db)
    assert "speculative" in await _columns(db, "observations")
    assert "draft" not in await _columns(db, "observations")
    assert "speculative" in await _columns(db, "speculative_claims")
    assert "draft" not in await _columns(db, "speculative_claims")


@pytest.mark.asyncio
async def test_idempotent_on_already_renamed(db):
    await M37.up(db)
    # Second application: column already named ``draft`` -> clean no-op.
    await M37.up(db)
    cols = await _columns(db, "procedural_memory")
    assert "draft" in cols
    assert "speculative" not in cols


@pytest.mark.asyncio
async def test_skips_when_base_table_absent(tmp_path):
    """The runner applies migrations against a bare DB (no base tables); 0037
    must skip cleanly rather than fail on a missing table."""
    async with aiosqlite.connect(str(tmp_path / "bare.db")) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("CREATE TABLE schema_migrations (version TEXT)")
        await conn.commit()
        await M37.up(conn)  # must not raise (no such table: procedural_memory)
