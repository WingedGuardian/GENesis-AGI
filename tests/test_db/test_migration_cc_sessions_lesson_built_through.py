"""Migration ``20261116040450_cc_sessions_lesson_built_through`` (#4807)."""

from __future__ import annotations

import importlib

import aiosqlite
import pytest

migration = importlib.import_module(
    "genesis.db.migrations.20261116040450_cc_sessions_lesson_built_through"
)


async def _cols(db: aiosqlite.Connection) -> list[str]:
    cur = await db.execute("PRAGMA table_info(cc_sessions)")
    return [row[1] for row in await cur.fetchall()]


@pytest.mark.asyncio
async def test_up_adds_the_column_and_is_idempotent():
    db = await aiosqlite.connect(":memory:")
    try:
        await db.execute("CREATE TABLE cc_sessions (id TEXT PRIMARY KEY, cc_session_id TEXT)")
        await db.execute("INSERT INTO cc_sessions VALUES ('a', 'b')")
        await migration.up(db)
        await migration.up(db)
        assert (await _cols(db))[-1] == "lesson_built_through"
        row = await (await db.execute("SELECT lesson_built_through FROM cc_sessions")).fetchone()
        assert row[0] is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_up_skips_a_database_without_the_table():
    db = await aiosqlite.connect(":memory:")
    try:
        await migration.up(db)
        cur = await db.execute("SELECT name FROM sqlite_master WHERE name='cc_sessions'")
        assert await cur.fetchone() is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_the_base_schema_carries_it():
    from genesis.db.schema import create_all_tables

    db = await aiosqlite.connect(":memory:")
    try:
        await create_all_tables(db)
        assert "lesson_built_through" in await _cols(db)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_advance_is_compare_and_set():
    from genesis.db.crud import cc_sessions as crud
    from genesis.db.schema import create_all_tables

    db = await aiosqlite.connect(":memory:")
    try:
        await create_all_tables(db)
        await db.execute(
            "INSERT INTO cc_sessions (id, session_type, model, started_at, last_activity_at, status)"
            " VALUES ('s', 'foreground', 'm', 'x', 'x', 'active')"
        )
        assert await crud.advance_lesson_built_through(db, "s", expected=None, new="t1")
        assert not await crud.advance_lesson_built_through(db, "s", expected=None, new="t2")
        assert await crud.get_lesson_built_through(db, "s") == "t1"
        assert await crud.advance_lesson_built_through(db, "s", expected="t1", new="t2")
        assert await crud.get_lesson_built_through(db, "missing") is None
    finally:
        await db.close()
