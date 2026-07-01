"""Migration 0042 — create attention_events (the attention-engine SHADOW store).

Stores attention decisions + references + labels only; NEVER ambient transcript text
(firewall). Verifies the table/columns/indexes, idempotency, and down().
"""

from __future__ import annotations

import importlib

import aiosqlite
import pytest

M42 = importlib.import_module("genesis.db.migrations.0042_attention_events")


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
async def test_creates_table_with_expected_columns_and_no_text(db):
    await M42.up(db)
    cols = await _columns(db, "attention_events")
    assert {
        "id", "ts", "session_id", "activation", "score", "triggers_fired",
        "suppressors", "window_ref", "mode_state", "clarity", "l15_verdict",
        "acceptance_signal", "snapshot_id", "config_version", "created_at",
    } <= cols
    assert "text" not in cols  # firewall: no transcript column exists at all


@pytest.mark.asyncio
async def test_creates_indexes(db):
    await M42.up(db)
    idx = await _indexes(db, "attention_events")
    assert {"idx_attention_events_session", "idx_attention_events_ts",
            "idx_attention_events_unlabeled"} <= idx


@pytest.mark.asyncio
async def test_idempotent_on_rerun(db):
    await M42.up(db)
    await M42.up(db)  # CREATE IF NOT EXISTS -> must not raise
    assert await _table_exists(db, "attention_events")


@pytest.mark.asyncio
async def test_down_drops_table(db):
    await M42.up(db)
    await M42.down(db)
    assert not await _table_exists(db, "attention_events")
