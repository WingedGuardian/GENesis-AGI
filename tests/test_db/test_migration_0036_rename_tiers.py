"""Migration 0036 — rename procedure activation tiers L1-L4 -> names.

L1-L4 are inverted vs every other ladder in Genesis (L1 = most-proven). This
one-time migration rewrites the ``activation_tier`` column AND the legacy L#
values embedded in the ``promotion_history`` JSON audit trail to the
self-documenting names CORE/ADVISORY/LIBRARY/DORMANT, while preserving non-tier
history values (e.g. ``"quarantined"``). Value-only — no schema change.
"""

from __future__ import annotations

import importlib
import json

import aiosqlite
import pytest

from genesis.db.crud import procedural

M36 = importlib.import_module("genesis.db.migrations.0036_rename_procedure_tiers")

_PROC = dict(
    principle="p", steps=["s"], tools_used=[], context_tags=[],
    created_at="2026-01-01T00:00:00",
)


@pytest.fixture
async def db(tmp_path):
    from genesis.db.schema import create_all_tables

    async with aiosqlite.connect(str(tmp_path / "t.db")) as conn:
        conn.row_factory = aiosqlite.Row
        await create_all_tables(conn)
        await conn.commit()
        yield conn


async def _tier(db, pid: str) -> str:
    cur = await db.execute(
        "SELECT activation_tier FROM procedural_memory WHERE id = ?", (pid,)
    )
    return (await cur.fetchone())[0]


async def _history(db, pid: str):
    cur = await db.execute(
        "SELECT promotion_history FROM procedural_memory WHERE id = ?", (pid,)
    )
    raw = (await cur.fetchone())[0]
    return json.loads(raw) if raw else None


@pytest.mark.asyncio
async def test_rewrites_activation_tier_to_names(db):
    await procedural.create(db, id="c", task_type="tc", activation_tier="L1", **_PROC)
    await procedural.create(db, id="a", task_type="ta", activation_tier="L2", **_PROC)
    await procedural.create(db, id="l", task_type="tl", activation_tier="L3", **_PROC)
    await procedural.create(db, id="d", task_type="td", activation_tier="L4", **_PROC)
    await db.commit()

    await M36.up(db)

    assert await _tier(db, "c") == "CORE"
    assert await _tier(db, "a") == "ADVISORY"
    assert await _tier(db, "l") == "LIBRARY"
    assert await _tier(db, "d") == "DORMANT"


@pytest.mark.asyncio
async def test_rewrites_promotion_history_json_preserving_non_tier(db):
    history = [
        {"from_tier": "L4", "to_tier": "L3", "at": "t1", "reason": "promote"},
        {"from_tier": "L3", "to_tier": "L1", "at": "t2", "reason": "promote"},
        {"from_tier": "L1", "to_tier": "quarantined", "at": "t3", "reason": "drift"},
    ]
    await procedural.create(
        db, id="h", task_type="th", activation_tier="L1",
        promotion_history=json.dumps(history), **_PROC,
    )
    await db.commit()

    await M36.up(db)

    got = await _history(db, "h")
    assert got[0] == {"from_tier": "DORMANT", "to_tier": "LIBRARY", "at": "t1", "reason": "promote"}
    assert got[1] == {"from_tier": "LIBRARY", "to_tier": "CORE", "at": "t2", "reason": "promote"}
    # Non-tier history values (e.g. "quarantined") are preserved verbatim.
    assert got[2] == {"from_tier": "CORE", "to_tier": "quarantined", "at": "t3", "reason": "drift"}


@pytest.mark.asyncio
async def test_distribution_preserved_no_l_values_remain(db):
    seed = {"L1": 1, "L2": 2, "L3": 3, "L4": 4}
    n = 0
    for tier, count in seed.items():
        for _ in range(count):
            await procedural.create(db, id=f"{tier}-{n}", task_type=f"t{n}", activation_tier=tier, **_PROC)
            n += 1
    await db.commit()

    await M36.up(db)

    cur = await db.execute(
        "SELECT activation_tier, COUNT(*) FROM procedural_memory GROUP BY activation_tier"
    )
    dist = {row[0]: row[1] for row in await cur.fetchall()}
    assert dist == {"CORE": 1, "ADVISORY": 2, "LIBRARY": 3, "DORMANT": 4}
    # No legacy L# values anywhere.
    cur = await db.execute(
        "SELECT COUNT(*) FROM procedural_memory WHERE activation_tier IN ('L1','L2','L3','L4')"
    )
    assert (await cur.fetchone())[0] == 0


@pytest.mark.asyncio
async def test_idempotent_on_rerun(db):
    await procedural.create(
        db, id="r", task_type="tr", activation_tier="L3",
        promotion_history=json.dumps([{"from_tier": "L4", "to_tier": "L3", "at": "t", "reason": "x"}]),
        **_PROC,
    )
    await db.commit()

    await M36.up(db)
    await M36.up(db)  # re-run: nothing matches L# anymore -> no-op

    assert await _tier(db, "r") == "LIBRARY"
    got = await _history(db, "r")
    assert got[0]["from_tier"] == "DORMANT"
    assert got[0]["to_tier"] == "LIBRARY"


@pytest.mark.asyncio
async def test_null_promotion_history_does_not_crash(db):
    await procedural.create(db, id="nullh", task_type="tn", activation_tier="L2", **_PROC)
    await db.commit()
    await M36.up(db)
    assert await _tier(db, "nullh") == "ADVISORY"
    assert await _history(db, "nullh") is None


@pytest.mark.asyncio
async def test_skips_when_base_table_absent(tmp_path):
    """The runner applies migrations against a bare DB (no base tables); 0036
    must skip cleanly rather than fail on a missing table."""
    async with aiosqlite.connect(str(tmp_path / "bare.db")) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("CREATE TABLE schema_migrations (version TEXT)")
        await conn.commit()
        await M36.up(conn)  # must not raise (no such table: procedural_memory)
