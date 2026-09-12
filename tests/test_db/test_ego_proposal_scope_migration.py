"""Migration 0078 — scope columns + grandfather backfill: idempotence, scoping.

Runner owns the transaction; these tests read back on the same connection.
"""

from __future__ import annotations

import importlib

import aiosqlite
import pytest

from genesis.db.crud import ego as ego_crud
from genesis.db.schema import TABLES

M78 = importlib.import_module("genesis.db.migrations.0078_ego_proposal_scope")


@pytest.fixture
async def db():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute(TABLES["ego_proposals"])
        await conn.execute(TABLES["ego_proposal_revisions"])
        yield conn


async def _cols(db, table):
    cur = await db.execute(f"PRAGMA table_info({table})")
    return {r[1] for r in await cur.fetchall()}


async def _seed(db, *, id, ego_source, status="pending", verdict="pass"):
    await ego_crud.create_proposal(
        db,
        id=id,
        action_type="investigate",
        content=f"p {id}",
        status=status,
        ego_source=ego_source,
        realist_verdict=verdict,
    )


@pytest.mark.asyncio
async def test_columns_added(db):
    await M78.up(db)
    assert {"scope", "scope_revision"} <= await _cols(db, "ego_proposals")
    assert "scope" in await _cols(db, "ego_proposal_revisions")


@pytest.mark.asyncio
async def test_backfill_grandfathers_passed_genesis_pending(db):
    await _seed(db, id="g_pass", ego_source="genesis_ego_cycle", verdict="pass")
    await _seed(db, id="g_amend", ego_source="genesis_ego_cycle", verdict="amend")
    await _seed(db, id="u_pass", ego_source="user_ego_cycle", verdict="pass")
    await _seed(db, id="g_done", ego_source="genesis_ego_cycle", status="executed", verdict="pass")
    await M78.up(db)

    async def scope(i):
        return (await ego_crud.get_proposal(db, i))["scope"]

    assert await scope("g_pass") == "operate"  # grandfathered
    assert await scope("g_amend") is None  # not a clean pass → left unjudged
    assert await scope("u_pass") is None  # user-ego never scoped
    assert await scope("g_done") is None  # not pending


@pytest.mark.asyncio
async def test_idempotent(db):
    await _seed(db, id="g1", ego_source="genesis_ego_cycle", verdict="pass")
    await M78.up(db)
    await M78.up(db)  # second run must not raise or double-touch
    assert (await ego_crud.get_proposal(db, "g1"))["scope"] == "operate"


@pytest.mark.asyncio
async def test_fresh_db_without_table_is_noop(db):
    await db.execute("DROP TABLE ego_proposals")
    await db.execute("DROP TABLE ego_proposal_revisions")
    await M78.up(db)  # must not raise
