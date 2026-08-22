"""Migration 0077 — revalidate_at backfill: scoping + format parity + idempotence.

The migration does NOT commit (the runner owns the transaction); these tests
read back on the same aiosqlite connection. Wall-clock-independent: stamps are
asserted relative to the seeded created_at values, never to now().
"""

from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta

import aiosqlite
import pytest

from genesis.db.crud import ego as ego_crud
from genesis.db.schema import TABLES

M77 = importlib.import_module("genesis.db.migrations.0077_backfill_revalidate_at")

_CREATED = "2026-07-28T02:56:31.450323+00:00"


@pytest.fixture
async def db():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute(TABLES["ego_proposals"])
        yield conn


async def _seed(db, *, id, status="pending", urgency="normal", action_type="investigate"):
    await ego_crud.create_proposal(
        db,
        id=id,
        action_type=action_type,
        content=f"proposal {id}",
        urgency=urgency,
        created_at=_CREATED,
    )
    if status != "pending":
        await db.execute("UPDATE ego_proposals SET status = ? WHERE id = ?", (status, id))


async def _reval(db, id):
    cur = await db.execute(
        "SELECT revalidate_at, last_validated_at FROM ego_proposals WHERE id = ?",
        (id,),
    )
    return await cur.fetchone()


@pytest.mark.asyncio
async def test_backfills_pending_per_urgency(db):
    await _seed(db, id="p_norm", urgency="normal")
    await _seed(db, id="p_crit", urgency="critical")
    await M77.up(db)

    created_dt = datetime.fromisoformat(_CREATED)
    row = await _reval(db, "p_norm")
    assert row["revalidate_at"] == (created_dt + timedelta(hours=72)).isoformat()
    assert row["last_validated_at"] == _CREATED
    row = await _reval(db, "p_crit")
    assert row["revalidate_at"] == (created_dt + timedelta(hours=6)).isoformat()


@pytest.mark.asyncio
async def test_iso_format_parity(db):
    """Stamps must compare correctly against datetime.now(UTC).isoformat()."""
    await _seed(db, id="p1")
    await M77.up(db)
    row = await _reval(db, "p1")
    # ISO-8601 'T'-separated with offset — same shape as the organic stamps,
    # so the reconcile string comparison is well-ordered.
    assert "T" in row["revalidate_at"]
    assert row["revalidate_at"].endswith("+00:00")
    assert row["revalidate_at"] < datetime.now(UTC).isoformat()


@pytest.mark.asyncio
async def test_informational_and_non_pending_untouched(db):
    await _seed(db, id="p_j9", action_type="j9_regression")
    await _seed(db, id="p_done", status="approved")
    await M77.up(db)
    assert (await _reval(db, "p_j9"))["revalidate_at"] is None
    assert (await _reval(db, "p_done"))["revalidate_at"] is None


@pytest.mark.asyncio
async def test_idempotent_and_preserves_organic_stamps(db):
    await _seed(db, id="p1")
    await db.execute("UPDATE ego_proposals SET revalidate_at = 'ORGANIC' WHERE id = 'p1'")
    await _seed(db, id="p2")
    await M77.up(db)
    stamped = (await _reval(db, "p2"))["revalidate_at"]
    await M77.up(db)  # second run: no-op
    assert (await _reval(db, "p1"))["revalidate_at"] == "ORGANIC"
    assert (await _reval(db, "p2"))["revalidate_at"] == stamped


@pytest.mark.asyncio
async def test_pre_0071_db_without_column_is_noop(db):
    await db.execute("ALTER TABLE ego_proposals DROP COLUMN revalidate_at")
    await M77.up(db)  # must not raise
