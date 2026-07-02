"""Migration 0044 — create capability_shadow_events (WS5 Discord gate SHADOW store).

Verifies the table/columns/indexes, idempotency, and down().
"""

from __future__ import annotations

import importlib

import aiosqlite
import pytest

M44 = importlib.import_module("genesis.db.migrations.0044_capability_shadow_events")


async def _columns(db: aiosqlite.Connection, table: str) -> set[str]:
    cur = await db.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in await cur.fetchall()}


async def _indexes(db: aiosqlite.Connection, table: str) -> set[str]:
    cur = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=?", (table,)
    )
    return {row[0] for row in await cur.fetchall()}


async def _table_exists(db: aiosqlite.Connection, table: str) -> bool:
    cur = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    )
    return await cur.fetchone() is not None


@pytest.fixture
async def db(tmp_path):
    async with aiosqlite.connect(str(tmp_path / "t.db")) as conn:
        conn.row_factory = aiosqlite.Row
        yield conn


@pytest.mark.asyncio
async def test_creates_table_with_expected_columns(db):
    await M44.up(db)
    cols = await _columns(db, "capability_shadow_events")
    assert {
        "id", "observed_at", "path", "channel", "cell_domain", "cell_verb",
        "cell_risk_class", "cell_state", "would_hold", "target",
        "content_preview", "content_hash",
    } == cols  # exact column set — no full-content column leaks in


@pytest.mark.asyncio
async def test_creates_indexes(db):
    await M44.up(db)
    idx = await _indexes(db, "capability_shadow_events")
    assert {
        "idx_capability_shadow_events_observed_at",
        "idx_capability_shadow_events_cell",
    } <= idx


@pytest.mark.asyncio
async def test_idempotent_on_rerun(db):
    await M44.up(db)
    await M44.up(db)  # CREATE IF NOT EXISTS -> must not raise
    assert await _table_exists(db, "capability_shadow_events")


@pytest.mark.asyncio
async def test_down_drops_table(db):
    await M44.up(db)
    await M44.down(db)
    assert not await _table_exists(db, "capability_shadow_events")
