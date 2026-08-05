"""Migration 0075 — add ``follow_ups.revisit_condition``.

Covers the legacy upgrade, idempotency, the double-application path
(``create_all_tables`` then the numbered runner — the fresh-DB boot case that a
naive ALTER would crash on), the no-table no-op, ``down``, and — the reason the
column is declared last in the canonical DDL — fresh/migrated column-ORDER parity.
"""

from __future__ import annotations

import importlib

import aiosqlite
import pytest

M75 = importlib.import_module("genesis.db.migrations.0075_follow_up_revisit_condition")

# follow_ups as it existed BEFORE this column (pre-0075 installs).
_LEGACY_DDL = """
    CREATE TABLE follow_ups (
        id               TEXT PRIMARY KEY,
        source           TEXT NOT NULL,
        source_session   TEXT,
        content          TEXT NOT NULL,
        reason           TEXT,
        strategy         TEXT NOT NULL CHECK (
            strategy IN ('scheduled_task', 'surplus_task', 'ego_judgment', 'user_input_needed')
        ),
        scheduled_at     TEXT,
        status           TEXT NOT NULL DEFAULT 'pending' CHECK (
            status IN ('pending', 'scheduled', 'in_progress', 'completed', 'failed', 'blocked')
        ),
        linked_task_id   TEXT,
        priority         TEXT NOT NULL DEFAULT 'medium' CHECK (
            priority IN ('low', 'medium', 'high', 'critical')
        ),
        created_at       TEXT NOT NULL,
        completed_at     TEXT,
        resolution_notes TEXT,
        blocked_reason   TEXT,
        escalated_to     TEXT,
        verified_at      TEXT,
        verification_notes TEXT,
        pinned           INTEGER NOT NULL DEFAULT 0,
        kind             TEXT NOT NULL DEFAULT 'follow_up' CHECK (
            kind IN ('follow_up', 'tabled')
        ),
        domain           TEXT CHECK (
            domain IN ('internal', 'user_world')
        ),
        goal_id          TEXT,
        dedup_key        TEXT
    )
"""


async def _cols(db: aiosqlite.Connection) -> list[str]:
    cur = await db.execute("PRAGMA table_info(follow_ups)")
    return [row[1] for row in await cur.fetchall()]


@pytest.mark.asyncio
async def test_up_adds_column_to_legacy_db(tmp_path):
    async with aiosqlite.connect(str(tmp_path / "t.db")) as db:
        await db.execute(_LEGACY_DDL)
        assert "revisit_condition" not in await _cols(db)
        await M75.up(db)
        assert "revisit_condition" in await _cols(db)


@pytest.mark.asyncio
async def test_up_is_idempotent(tmp_path):
    async with aiosqlite.connect(str(tmp_path / "t.db")) as db:
        await db.execute(_LEGACY_DDL)
        await M75.up(db)
        await M75.up(db)  # second run must not raise
        assert (await _cols(db)).count("revisit_condition") == 1


@pytest.mark.asyncio
async def test_double_path_after_create_all_tables(tmp_path):
    """create_all_tables already carries the column; the runner must then no-op
    (BLOCKER: a naive ALTER would raise 'duplicate column' on every fresh boot)."""
    from genesis.db.schema import create_all_tables

    async with aiosqlite.connect(str(tmp_path / "t.db")) as db:
        await create_all_tables(db)
        assert "revisit_condition" in await _cols(db)
        await M75.up(db)  # must not raise "duplicate column"
        assert (await _cols(db)).count("revisit_condition") == 1


@pytest.mark.asyncio
async def test_up_no_ops_when_table_absent(tmp_path):
    """A DB with no follow_ups table yet must not raise (fresh-install ordering)."""
    async with aiosqlite.connect(str(tmp_path / "t.db")) as db:
        await M75.up(db)  # no-op, no raise
        cur = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='follow_ups'"
        )
        assert await cur.fetchone() is None


@pytest.mark.asyncio
async def test_fresh_and_migrated_column_order_identical(tmp_path):
    """revisit_condition is declared LAST precisely so both paths agree byte-for-byte
    — ALTER appends, so a mid-table CREATE would diverge from an upgraded DB."""
    from genesis.db.schema import create_all_tables

    async with aiosqlite.connect(str(tmp_path / "fresh.db")) as db:
        await create_all_tables(db)
        fresh = await _cols(db)
    async with aiosqlite.connect(str(tmp_path / "migrated.db")) as db:
        await db.execute(_LEGACY_DDL)
        await M75.up(db)
        migrated = await _cols(db)

    assert fresh == migrated
    assert fresh[-1] == "revisit_condition"


@pytest.mark.asyncio
async def test_down_removes_column(tmp_path):
    async with aiosqlite.connect(str(tmp_path / "t.db")) as db:
        await db.execute(_LEGACY_DDL)
        await M75.up(db)
        await M75.down(db)
        assert "revisit_condition" not in await _cols(db)
