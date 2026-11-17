"""Migration ``20261116034039_procedural_created_session_id`` (#4717)."""

from __future__ import annotations

import importlib

import aiosqlite
import pytest

migration = importlib.import_module(
    "genesis.db.migrations.20261116034039_procedural_created_session_id"
)


async def _cols(db: aiosqlite.Connection) -> list[str]:
    cur = await db.execute("PRAGMA table_info(procedural_memory)")
    return [row[1] for row in await cur.fetchall()]


@pytest.mark.asyncio
async def test_up_adds_the_column_to_a_table_without_it_and_is_idempotent():
    db = await aiosqlite.connect(":memory:")
    try:
        await db.execute(
            "CREATE TABLE procedural_memory (id TEXT PRIMARY KEY, source TEXT, created_at TEXT)"
        )
        await db.execute("INSERT INTO procedural_memory VALUES ('a', '{}', '2026-11-01')")
        await migration.up(db)
        await migration.up(db)
        assert (await _cols(db))[-1] == "created_session_id"
        row = await (
            await db.execute("SELECT created_session_id FROM procedural_memory")
        ).fetchone()
        assert row[0] is None  # no backfill
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_up_skips_a_database_without_the_table():
    db = await aiosqlite.connect(":memory:")
    try:
        await migration.up(db)
        cur = await db.execute("SELECT name FROM sqlite_master WHERE name='procedural_memory'")
        assert await cur.fetchone() is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_the_base_schema_carries_it():
    from genesis.db.schema import create_all_tables

    db = await aiosqlite.connect(":memory:")
    try:
        await create_all_tables(db)
        assert "created_session_id" in await _cols(db)
    finally:
        await db.close()
