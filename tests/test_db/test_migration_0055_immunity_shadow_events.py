"""Migration 0055 — create immunity_shadow_events (WS-3 B1 immunity SHADOW store).

Verifies the table/columns/indexes, idempotency, and down().
"""

from __future__ import annotations

import importlib

import aiosqlite
import pytest

M55 = importlib.import_module("genesis.db.migrations.0055_immunity_shadow_events")


async def _columns(db: aiosqlite.Connection, table: str) -> set[str]:
    cur = await db.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in await cur.fetchall()}


async def _indexes(db: aiosqlite.Connection, table: str) -> set[str]:
    cur = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=?", (table,)
    )
    return {row[0] for row in await cur.fetchall()}


async def _table_exists(db: aiosqlite.Connection, table: str) -> bool:
    cur = await db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
    return await cur.fetchone() is not None


@pytest.fixture
async def db(tmp_path):
    async with aiosqlite.connect(str(tmp_path / "t.db")) as conn:
        conn.row_factory = aiosqlite.Row
        yield conn


@pytest.mark.asyncio
async def test_creates_table_with_expected_columns(db):
    await M55.up(db)
    cols = await _columns(db, "immunity_shadow_events")
    assert {
        "id",
        "observed_at",
        "gate",
        "mode",
        "origin_class",
        "would_block",
        "source_kind",
        "source_ref",
        "detail",
        "process",
    } == cols  # exact column set — no recalled-content column leaks in


@pytest.mark.asyncio
async def test_creates_indexes(db):
    await M55.up(db)
    idx = await _indexes(db, "immunity_shadow_events")
    assert {
        "idx_immunity_shadow_events_observed_at",
        "idx_immunity_shadow_events_gate",
    } <= idx


@pytest.mark.asyncio
async def test_idempotent_on_rerun(db):
    await M55.up(db)
    await M55.up(db)  # CREATE IF NOT EXISTS -> must not raise
    assert await _table_exists(db, "immunity_shadow_events")


@pytest.mark.asyncio
async def test_down_drops_table(db):
    await M55.up(db)
    await M55.down(db)
    assert not await _table_exists(db, "immunity_shadow_events")


@pytest.mark.asyncio
async def test_matches_schema_mirror(db):
    """The migration and the fresh-DB mirror in _tables.py must agree."""
    from genesis.db.schema._tables import TABLES

    await M55.up(db)
    migrated = await _columns(db, "immunity_shadow_events")

    async with aiosqlite.connect(":memory:") as fresh:
        await fresh.execute(TABLES["immunity_shadow_events"])
        cur = await fresh.execute("PRAGMA table_info(immunity_shadow_events)")
        mirror = {row[1] for row in await cur.fetchall()}
    assert migrated == mirror
