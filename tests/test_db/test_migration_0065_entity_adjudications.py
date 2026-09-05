"""Migration 0065 — create ``entity_adjudications`` (entity-node merge ledger).

Verifies the full column set, the verdict index, idempotency, the verdict
CHECK, the pair_key UNIQUE dedupe key, fresh-canonical parity with
``_tables.py``, and ``down``.
"""

from __future__ import annotations

import importlib
import sqlite3

import aiosqlite
import pytest

M65 = importlib.import_module("genesis.db.migrations.0065_entity_adjudications")

_EXPECTED_COLUMNS = {
    "id",
    "pair_key",
    "entity_a",
    "entity_b",
    "loser_id",
    "survivor_id",
    "verdict",
    "reasoning",
    "provider",
    "mode",
    "norm_a",
    "norm_b",
    "updated_a",
    "updated_b",
    "created_at",
    "applied_at",
}

# The canonical _tables.py schema is the CUMULATIVE column set: migration 0065's
# original columns PLUS approved_at/approved_by, which a LATER migration
# (0093_entity_adjudication_approval) adds for the human-approval gate. Migration
# 0065 is frozen history and must never be edited retroactively
# (numbered_migration_self_contained), so its up() still builds only
# _EXPECTED_COLUMNS — the parity check below asserts the canonical set is exactly
# 0065's set plus these two approval columns, not that the two are identical.
_APPROVAL_COLUMNS = {"approved_at", "approved_by"}
# Added by 20260905155727_entity_adjudication_policy (MW-3 PR-2b).
_POLICY_COLUMNS = {"policy"}
_CANONICAL_COLUMNS = _EXPECTED_COLUMNS | _APPROVAL_COLUMNS | _POLICY_COLUMNS

_BASE_ROW = {
    "id": "a-1",
    "pair_key": "e1|e2",
    "entity_a": "e1",
    "entity_b": "e2",
    "verdict": "distinct",
    "created_at": "2026-07-17T12:00:00+00:00",
}


async def _columns(db: aiosqlite.Connection) -> set[str]:
    cur = await db.execute("PRAGMA table_info(entity_adjudications)")
    return {row[1] for row in await cur.fetchall()}


async def _insert(db: aiosqlite.Connection, **overrides) -> None:
    row = {**_BASE_ROW, **overrides}
    cols = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    await db.execute(
        f"INSERT INTO entity_adjudications ({cols}) VALUES ({marks})",  # noqa: S608 — test-local column names
        tuple(row.values()),
    )


@pytest.mark.asyncio
async def test_up_creates_table_with_full_column_set(tmp_path):
    async with aiosqlite.connect(str(tmp_path / "t.db")) as db:
        await M65.up(db)
        assert await _columns(db) == _EXPECTED_COLUMNS


@pytest.mark.asyncio
async def test_up_creates_verdict_index(tmp_path):
    async with aiosqlite.connect(str(tmp_path / "t.db")) as db:
        await M65.up(db)
        cur = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name='entity_adjudications' AND name LIKE 'idx_entity_adjud%'"
        )
        assert {row[0] for row in await cur.fetchall()} == {"idx_entity_adjud_verdict"}


@pytest.mark.asyncio
async def test_up_is_idempotent(tmp_path):
    async with aiosqlite.connect(str(tmp_path / "t.db")) as db:
        await M65.up(db)
        await M65.up(db)  # second run must not raise
        assert await _columns(db) == _EXPECTED_COLUMNS


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_verdict", ["approve", "keep", "", "MERGE"])
async def test_verdict_check_rejects(tmp_path, bad_verdict):
    async with aiosqlite.connect(str(tmp_path / "t.db")) as db:
        await M65.up(db)
        with pytest.raises(sqlite3.IntegrityError):
            await _insert(db, verdict=bad_verdict)


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", ["merge", "distinct", "proposed_merge", "stale"])
async def test_verdict_check_accepts_valid(tmp_path, verdict):
    async with aiosqlite.connect(str(tmp_path / "t.db")) as db:
        await M65.up(db)
        await _insert(db, id=f"a-{verdict}", pair_key=f"k-{verdict}", verdict=verdict)


@pytest.mark.asyncio
async def test_pair_key_unique(tmp_path):
    async with aiosqlite.connect(str(tmp_path / "t.db")) as db:
        await M65.up(db)
        await _insert(db)
        with pytest.raises(sqlite3.IntegrityError):
            await _insert(db, id="a-2")  # same pair_key, different id


@pytest.mark.asyncio
async def test_fresh_canonical_parity(tmp_path):
    """Canonical _tables.py = migration 0065's columns PLUS the approval columns.

    The fresh-install DDL carries the cumulative schema (0065 + the later
    0090 approval-gate migration), so it is NOT identical to frozen 0065 — it is
    exactly 0065's set plus approved_at/approved_by. Asserting the precise
    relationship (rather than equality) catches both a canonical schema that drops
    a 0065 column AND an approval column that never reached _tables.py.
    """
    from genesis.db.schema import TABLES

    async with aiosqlite.connect(str(tmp_path / "t.db")) as db:
        await db.execute(TABLES["entity_adjudications"])
        fresh_cols = await _columns(db)
    async with aiosqlite.connect(str(tmp_path / "m.db")) as db:
        await M65.up(db)
        migrated_cols = await _columns(db)
    assert migrated_cols == _EXPECTED_COLUMNS
    assert fresh_cols == _CANONICAL_COLUMNS
    assert fresh_cols == migrated_cols | _APPROVAL_COLUMNS | _POLICY_COLUMNS


@pytest.mark.asyncio
async def test_down_drops_table(tmp_path):
    async with aiosqlite.connect(str(tmp_path / "t.db")) as db:
        await M65.up(db)
        await M65.down(db)
        cur = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='entity_adjudications'"
        )
        assert await cur.fetchone() is None
