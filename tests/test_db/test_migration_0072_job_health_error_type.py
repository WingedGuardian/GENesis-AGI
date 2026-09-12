"""Migration 0072 — add ``job_health.error_type``.

Covers the legacy upgrade, idempotency, the double-application path
(``create_all_tables`` then the numbered runner), the no-table no-op, ``down``,
and — the reason this column is declared last — fresh/migrated column-ORDER
parity, so a positional reader can never disagree between a fresh clone and an
upgraded install.
"""

from __future__ import annotations

import importlib

import aiosqlite
import pytest

M72 = importlib.import_module("genesis.db.migrations.0072_job_health_error_type")

# job_health as it existed BEFORE this column (pre-0072 installs).
_LEGACY_DDL = """
    CREATE TABLE job_health (
        job_name         TEXT PRIMARY KEY,
        last_run         TEXT,
        last_success     TEXT,
        last_failure     TEXT,
        last_error       TEXT,
        consecutive_failures INTEGER NOT NULL DEFAULT 0,
        total_runs       INTEGER NOT NULL DEFAULT 0,
        total_successes  INTEGER NOT NULL DEFAULT 0,
        total_failures   INTEGER NOT NULL DEFAULT 0,
        updated_at       TEXT NOT NULL
    )
"""


async def _cols(db: aiosqlite.Connection) -> list[str]:
    cur = await db.execute("PRAGMA table_info(job_health)")
    return [row[1] for row in await cur.fetchall()]


@pytest.mark.asyncio
async def test_up_adds_column_to_legacy_db(tmp_path):
    async with aiosqlite.connect(str(tmp_path / "t.db")) as db:
        await db.execute(_LEGACY_DDL)
        assert "error_type" not in await _cols(db)
        await M72.up(db)
        assert "error_type" in await _cols(db)


@pytest.mark.asyncio
async def test_up_is_idempotent(tmp_path):
    async with aiosqlite.connect(str(tmp_path / "t.db")) as db:
        await db.execute(_LEGACY_DDL)
        await M72.up(db)
        await M72.up(db)  # second run must not raise
        assert (await _cols(db)).count("error_type") == 1


@pytest.mark.asyncio
async def test_double_path_after_create_all_tables(tmp_path):
    """create_all_tables already adds the column; the runner must then no-op."""
    from genesis.db.schema import create_all_tables

    async with aiosqlite.connect(str(tmp_path / "t.db")) as db:
        await create_all_tables(db)
        assert "error_type" in await _cols(db)
        await M72.up(db)  # must not raise "duplicate column"
        assert (await _cols(db)).count("error_type") == 1


@pytest.mark.asyncio
async def test_up_no_ops_when_table_absent(tmp_path):
    """A DB with no job_health table yet must not raise (fresh-install ordering)."""
    async with aiosqlite.connect(str(tmp_path / "t.db")) as db:
        await M72.up(db)  # no-op, no raise
        cur = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='job_health'"
        )
        assert await cur.fetchone() is None


@pytest.mark.asyncio
async def test_fresh_and_migrated_column_order_identical(tmp_path):
    """error_type is declared LAST precisely so both paths agree byte-for-byte —
    ALTER appends, so a mid-table CREATE would diverge from an upgraded DB."""
    from genesis.db.schema import create_all_tables

    async with aiosqlite.connect(str(tmp_path / "fresh.db")) as db:
        await create_all_tables(db)
        fresh = await _cols(db)
    async with aiosqlite.connect(str(tmp_path / "migrated.db")) as db:
        await db.execute(_LEGACY_DDL)
        await M72.up(db)
        migrated = await _cols(db)

    assert fresh == migrated
    assert fresh[-1] == "error_type"


@pytest.mark.asyncio
async def test_down_removes_column(tmp_path):
    async with aiosqlite.connect(str(tmp_path / "t.db")) as db:
        await db.execute(_LEGACY_DDL)
        await M72.up(db)
        await M72.down(db)
        assert "error_type" not in await _cols(db)
