"""Migration 0068 — graduation_events table + memory_metadata provenance columns.

Simulates a PRE-0068 database: legacy ``memory_metadata`` DDL copied from
``db/schema/_tables.py`` minus the five W0 columns, and no
``graduation_events`` table. Asserts ``up()`` creates the table + index and
adds the columns, is idempotent (second run leaves everything identical), and
that the CHECK / UNIQUE constraints actually enforce.

Also proves base-path parity (the #1123/#1127 ``schema_both_build_paths``
class): ``create_all_tables`` on the same legacy DB must produce the same
columns via ``_migrate_add_columns`` — an existing install that boots without
ever running the numbered-migration runner still gets the schema.

The migration does NOT commit (the runner owns the transaction); these tests
read back on the same aiosqlite connection.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import aiosqlite
import pytest

M68 = importlib.import_module("genesis.db.migrations.0068_voice_graduation")

pytestmark = pytest.mark.asyncio

_W0_COLUMNS = {
    "provenance_class",
    "trust_level",
    "attribution",
    "origin_ref",
    "capture_clarity",
}

# Current memory_metadata DDL from _tables.py WITHOUT the five W0 columns.
_LEGACY_MEMORY_METADATA = """
    CREATE TABLE memory_metadata (
        memory_id        TEXT PRIMARY KEY,
        created_at       TEXT NOT NULL,
        collection       TEXT NOT NULL DEFAULT 'episodic_memory',
        confidence       REAL,
        embedding_status TEXT NOT NULL DEFAULT 'embedded',
        memory_class     TEXT DEFAULT 'fact',
        wing             TEXT,
        room             TEXT,
        valid_at         TEXT,
        invalid_at       TEXT,
        source_subsystem TEXT,
        deprecated       INTEGER NOT NULL DEFAULT 0,
        dream_cycle_run_id TEXT,
        origin_class     TEXT
    )
"""


async def _legacy_db() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.execute(_LEGACY_MEMORY_METADATA)
    await conn.execute(
        "INSERT INTO memory_metadata (memory_id, created_at) VALUES ('m1', '2026-01-01')"
    )
    return conn


async def _columns(db: aiosqlite.Connection, table: str) -> set[str]:
    cursor = await db.execute(f"PRAGMA table_info({table})")  # noqa: S608 - fixed test identifiers
    return {row[1] for row in await cursor.fetchall()}


async def test_up_creates_table_index_and_columns():
    db = await _legacy_db()
    try:
        await M68.up(db)

        cursor = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='graduation_events'"
        )
        assert await cursor.fetchone() is not None

        cursor = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name='idx_graduation_events_disposition'"
        )
        assert await cursor.fetchone() is not None

        assert await _columns(db, "memory_metadata") >= _W0_COLUMNS
        # Existing data preserved, new columns NULL
        row = await (
            await db.execute("SELECT * FROM memory_metadata WHERE memory_id='m1'")
        ).fetchone()
        assert row["created_at"] == "2026-01-01"
        assert row["provenance_class"] is None
    finally:
        await db.close()


async def test_up_is_idempotent():
    db = await _legacy_db()
    try:
        await M68.up(db)
        await M68.up(db)  # must not raise (duplicate table/column/index)
        assert await _columns(db, "memory_metadata") >= _W0_COLUMNS
    finally:
        await db.close()


async def test_constraints_enforce():
    db = await _legacy_db()
    try:
        await M68.up(db)

        base = (
            "INSERT INTO graduation_events "
            "(id, event_id, schema_version, type, source, occurred_at, "
            " received_at, payload, provenance) "
            "VALUES (?, ?, 1, ?, 's', 't', 't', '{}', '{}')"
        )
        await db.execute(base, ("r1", "e1", "perk_up"))

        with pytest.raises(aiosqlite.IntegrityError):
            await db.execute(base, ("r2", "e2", "bogus_type"))
        with pytest.raises(aiosqlite.IntegrityError):
            await db.execute("UPDATE graduation_events SET disposition = 'bogus' WHERE id = 'r1'")
        # UNIQUE(event_id): a plain INSERT (no OR IGNORE) must raise
        with pytest.raises(aiosqlite.IntegrityError):
            await db.execute(base, ("r3", "e1", "perk_up"))
    finally:
        await db.close()


async def test_base_path_parity_on_legacy_db():
    """create_all_tables on a legacy DB yields the same schema (both build paths)."""
    from genesis.db.schema import create_all_tables

    db = await _legacy_db()
    try:
        await create_all_tables(db)

        assert await _columns(db, "memory_metadata") >= _W0_COLUMNS
        cursor = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='graduation_events'"
        )
        assert await cursor.fetchone() is not None
        # Legacy data survives the base path too
        row = await (
            await db.execute("SELECT created_at FROM memory_metadata WHERE memory_id='m1'")
        ).fetchone()
        assert row[0] == "2026-01-01"
    finally:
        await db.close()


def test_event_types_match_ddl_check_lists():
    """EVENT_TYPES (validator) must equal the CHECK list in BOTH DDL copies.

    SQLite cannot ALTER a CHECK constraint — existing installs keep the W0
    list forever. If a future PR extends EVENT_TYPES without a migration
    strategy for the frozen CHECK, validated events of the new type would be
    OR-IGNOREd on old DBs. insert_event now raises on that case; this test
    catches the drift at CI time instead.
    """
    import re

    from genesis.channels.voice.graduation import EVENT_TYPES
    from genesis.db.schema._tables import TABLES

    def check_list(ddl: str) -> tuple[str, ...]:
        m = re.search(r"type\s+TEXT\s+NOT\s+NULL\s+CHECK\s*\(type IN \(([^)]+)\)", ddl)
        assert m, "type CHECK not found in DDL"
        return tuple(v.strip().strip("'") for v in m.group(1).split(","))

    canonical = check_list(TABLES["graduation_events"])
    migration_src = Path(M68.__file__).read_text()
    migration = check_list(migration_src)

    assert canonical == EVENT_TYPES
    assert migration == EVENT_TYPES
