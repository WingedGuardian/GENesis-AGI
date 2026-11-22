"""Migration ``20261115161928_tco_error_class_recorded_at`` (#4722)."""

from __future__ import annotations

import importlib

import aiosqlite
import pytest

from genesis.db.schema import create_all_tables

migration = importlib.import_module("genesis.db.migrations.20261115161928_tco_error_class_recorded_at")


async def _cols(db: aiosqlite.Connection) -> list[str]:
    cur = await db.execute("PRAGMA table_info(tool_call_outcomes)")
    return [row[1] for row in await cur.fetchall()]


async def _legacy_db() -> aiosqlite.Connection:
    db = await aiosqlite.connect(":memory:")
    await db.execute(
        """CREATE TABLE tool_call_outcomes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            tool_name TEXT NOT NULL,
            file_path TEXT,
            success INTEGER NOT NULL DEFAULT 1,
            error_snippet TEXT,
            timestamp TEXT NOT NULL,
            tool_use_id TEXT
        )"""
    )
    await db.execute(
        "INSERT INTO tool_call_outcomes (tool_name, success, timestamp) VALUES ('Edit', 0, 't')"
    )
    await db.commit()
    return db


@pytest.mark.asyncio
async def test_up_appends_the_column_and_keeps_rows_null():
    db = await _legacy_db()
    try:
        await migration.up(db)
        await migration.up(db)  # idempotent
        assert (await _cols(db))[-2:] == ["error_class", "recorded_at"]
        cur = await db.execute("SELECT error_class, recorded_at FROM tool_call_outcomes")
        assert [tuple(r) for r in await cur.fetchall()] == [(None, None)]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_up_skips_a_database_without_the_table():
    db = await aiosqlite.connect(":memory:")
    try:
        await migration.up(db)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_fresh_and_migrated_shapes_match():
    fresh = await aiosqlite.connect(":memory:")
    legacy = await _legacy_db()
    try:
        await create_all_tables(fresh)
        await migration.up(legacy)
        assert await _cols(fresh) == await _cols(legacy)
    finally:
        await fresh.close()
        await legacy.close()


@pytest.mark.asyncio
async def test_up_is_a_no_op_after_the_base_path_built_the_table():
    """Startup order: create_all_tables runs before the numbered runner."""
    db = await aiosqlite.connect(":memory:")
    try:
        await create_all_tables(db)
        before = await _cols(db)
        await migration.up(db)
        assert await _cols(db) == before and before[-2:] == ["error_class", "recorded_at"]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_the_base_schema_path_adds_the_column_to_an_existing_table():
    db = await _legacy_db()
    try:
        await create_all_tables(db)
        assert (await _cols(db))[-2:] == ["error_class", "recorded_at"]
    finally:
        await db.close()
