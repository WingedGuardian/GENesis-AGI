"""0074 — memory_reconcile_runs table (Phase-1 repair-lane audit rows)."""

from __future__ import annotations

import importlib

import aiosqlite
import pytest

m0074 = importlib.import_module("genesis.db.migrations.0074_memory_reconcile_runs")

pytestmark = pytest.mark.asyncio


async def _tables(db) -> set[str]:
    cursor = await db.execute("SELECT name FROM sqlite_master WHERE type='table'")
    return {r[0] for r in await cursor.fetchall()}


async def _indexes(db) -> set[str]:
    cursor = await db.execute("SELECT name FROM sqlite_master WHERE type='index'")
    return {r[0] for r in await cursor.fetchall()}


async def test_up_creates_table_and_indexes_idempotently():
    async with aiosqlite.connect(":memory:") as db:
        await m0074.up(db)
        assert "memory_reconcile_runs" in await _tables(db)
        assert {"idx_mrr_created", "idx_mrr_status"} <= await _indexes(db)
        await m0074.up(db)  # idempotent — IF NOT EXISTS throughout
        # status CHECK enforced
        with pytest.raises(aiosqlite.IntegrityError):
            await db.execute("INSERT INTO memory_reconcile_runs (id, status) VALUES ('x', 'bogus')")


async def test_down_removes_everything():
    async with aiosqlite.connect(":memory:") as db:
        await m0074.up(db)
        await m0074.down(db)
        assert "memory_reconcile_runs" not in await _tables(db)
        assert not {"idx_mrr_created", "idx_mrr_status"} & await _indexes(db)


async def test_matches_schema_mirror():
    """The migration's DDL and the _tables.py mirror must agree column-for-column
    (schema-both-build-paths): a fresh install (create_all_tables) and an
    upgraded install (0074) end at the same shape."""
    from genesis.db.schema._tables import TABLES

    async with aiosqlite.connect(":memory:") as via_migration:
        await m0074.up(via_migration)
        cursor = await via_migration.execute("PRAGMA table_info(memory_reconcile_runs)")
        mig_cols = [(r[1], r[2]) for r in await cursor.fetchall()]

    async with aiosqlite.connect(":memory:") as via_schema:
        await via_schema.execute(TABLES["memory_reconcile_runs"])
        cursor = await via_schema.execute("PRAGMA table_info(memory_reconcile_runs)")
        schema_cols = [(r[1], r[2]) for r in await cursor.fetchall()]

    assert mig_cols == schema_cols
