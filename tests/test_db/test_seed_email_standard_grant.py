"""Tests for migration 0032 — the WS-8 Option-B deploy seed.

Pre-grants email:send:standard, idempotently, without overriding an owner-set
cell. Depends on capability_grants (migration 0030), set up in the fixture.
"""

from __future__ import annotations

import importlib

import aiosqlite
import pytest

MIG_0030 = importlib.import_module("genesis.db.migrations.0030_capability_grants")
MIG_0032 = importlib.import_module("genesis.db.migrations.0032_seed_email_standard_grant")


@pytest.fixture
async def db(tmp_path):
    conn = await aiosqlite.connect(str(tmp_path / "t.db"))
    conn.row_factory = aiosqlite.Row
    await MIG_0030.up(conn)  # capability_grants
    await conn.commit()
    yield conn
    await conn.close()


@pytest.mark.asyncio
async def test_seeds_standard_grant(db):
    await MIG_0032.up(db)
    await db.commit()
    cur = await db.execute(
        "SELECT state FROM capability_grants WHERE id = 'email:send:standard'"
    )
    assert (await cur.fetchone())["state"] == "granted"


@pytest.mark.asyncio
async def test_seed_is_idempotent(db):
    await MIG_0032.up(db)
    await MIG_0032.up(db)
    await db.commit()
    cur = await db.execute(
        "SELECT COUNT(*) FROM capability_grants WHERE id = 'email:send:standard'"
    )
    assert (await cur.fetchone())[0] == 1


@pytest.mark.asyncio
async def test_seed_does_not_override_owner_revoke(db):
    # Owner already denied the cell — the seed must not re-grant it.
    await db.execute(
        "INSERT INTO capability_grants "
        "(id, domain, verb, risk_class, state, created_at, updated_at) "
        "VALUES ('email:send:standard','email','send','standard',"
        "'denied_permanent','t','t')"
    )
    await db.commit()
    await MIG_0032.up(db)
    await db.commit()
    cur = await db.execute(
        "SELECT state FROM capability_grants WHERE id = 'email:send:standard'"
    )
    assert (await cur.fetchone())["state"] == "denied_permanent"  # unchanged
