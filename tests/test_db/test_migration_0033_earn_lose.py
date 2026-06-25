"""Tests for migration 0033 — WS-8 PR-D earn/lose schema.

Covers the two new ``capability_grants`` columns, the ``autonomous_email_sends``
ledger + indexes, idempotency, fresh-install parity (``create_all_tables``), the
seed-revert (pristine PR-C grant removed, owner-touched cell preserved), and down.
"""

from __future__ import annotations

import importlib

import aiosqlite
import pytest

M30 = importlib.import_module("genesis.db.migrations.0030_capability_grants")
M33 = importlib.import_module("genesis.db.migrations.0033_autonomy_earn_lose")


async def _cols(db: aiosqlite.Connection, table: str) -> set[str]:
    cur = await db.execute(f"PRAGMA table_info({table})")
    return {r[1] for r in await cur.fetchall()}


@pytest.fixture
async def db(tmp_path):
    async with aiosqlite.connect(str(tmp_path / "t.db")) as conn:
        conn.row_factory = aiosqlite.Row
        await M30.up(conn)
        await M33.up(conn)
        await conn.commit()
        yield conn


@pytest.mark.asyncio
async def test_adds_columns(db):
    cols = await _cols(db, "capability_grants")
    assert "weighted_corrections" in cols
    assert "last_decayed_at" in cols


@pytest.mark.asyncio
async def test_creates_ledger_table_and_index(db):
    cur = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name='autonomous_email_sends'"
    )
    assert await cur.fetchone() is not None
    cur = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='index' "
        "AND name='idx_autonomous_email_sends_cell'"
    )
    assert await cur.fetchone() is not None


@pytest.mark.asyncio
async def test_up_is_idempotent(tmp_path):
    async with aiosqlite.connect(str(tmp_path / "idem.db")) as conn:
        await M30.up(conn)
        await M33.up(conn)
        await M33.up(conn)  # PRAGMA-guarded ALTER + IF NOT EXISTS → must not raise
        await conn.commit()
        cur = await conn.execute("PRAGMA table_info(capability_grants)")
        names = [r[1] for r in await cur.fetchall()]
        assert names.count("weighted_corrections") == 1
        assert names.count("last_decayed_at") == 1


@pytest.mark.asyncio
async def test_fresh_install_parity(tmp_path):
    """create_all_tables (fresh-install path) yields the same columns + table."""
    from genesis.db.schema import create_all_tables

    async with aiosqlite.connect(str(tmp_path / "fresh.db")) as conn:
        conn.row_factory = aiosqlite.Row
        await create_all_tables(conn)
        await conn.commit()
        cols = await _cols(conn, "capability_grants")
        assert {"weighted_corrections", "last_decayed_at"} <= cols
        cur = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='autonomous_email_sends'"
        )
        assert await cur.fetchone() is not None


@pytest.mark.asyncio
async def test_revert_removes_pristine_seed(tmp_path):
    async with aiosqlite.connect(str(tmp_path / "seed.db")) as conn:
        conn.row_factory = aiosqlite.Row
        await M30.up(conn)
        # Mimic the 0032 Option-B seed: a pristine GRANTED standard cell.
        await conn.execute(
            "INSERT INTO capability_grants "
            "(id, domain, verb, risk_class, state, granted_at, created_at, updated_at) "
            "VALUES ('email:send:standard','email','send','standard','granted','t','t','t')"
        )
        await conn.commit()
        await M33.up(conn)
        await conn.commit()
        cur = await conn.execute(
            "SELECT id FROM capability_grants WHERE id='email:send:standard'"
        )
        assert await cur.fetchone() is None  # pristine seed removed → all-ASK


@pytest.mark.asyncio
async def test_revert_keeps_owner_touched_cell(tmp_path):
    async with aiosqlite.connect(str(tmp_path / "seed2.db")) as conn:
        conn.row_factory = aiosqlite.Row
        await M30.up(conn)
        # The owner has acted on this cell (evidence present) → must NOT be removed.
        await conn.execute(
            "INSERT INTO capability_grants "
            "(id, domain, verb, risk_class, state, successes, corrections, "
            " granted_at, created_at, updated_at) "
            "VALUES ('email:send:standard','email','send','standard','granted',3,1,'t','t','t')"
        )
        await conn.commit()
        await M33.up(conn)
        await conn.commit()
        cur = await conn.execute(
            "SELECT state FROM capability_grants WHERE id='email:send:standard'"
        )
        row = await cur.fetchone()
        assert row is not None and row["state"] == "granted"


@pytest.mark.asyncio
async def test_down_drops_ledger(tmp_path):
    async with aiosqlite.connect(str(tmp_path / "down.db")) as conn:
        await M30.up(conn)
        await M33.up(conn)
        await M33.down(conn)
        await conn.commit()
        cur = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='autonomous_email_sends'"
        )
        assert await cur.fetchone() is None
