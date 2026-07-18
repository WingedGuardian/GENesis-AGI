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
    """_tables.py and the migration must build the identical column set."""
    from genesis.db.schema import TABLES

    async with aiosqlite.connect(str(tmp_path / "t.db")) as db:
        await db.execute(TABLES["entity_adjudications"])
        fresh_cols = await _columns(db)
    async with aiosqlite.connect(str(tmp_path / "m.db")) as db:
        await M65.up(db)
        migrated_cols = await _columns(db)
    assert fresh_cols == migrated_cols == _EXPECTED_COLUMNS


@pytest.mark.asyncio
async def test_down_drops_table(tmp_path):
    async with aiosqlite.connect(str(tmp_path / "t.db")) as db:
        await M65.up(db)
        await M65.down(db)
        cur = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='entity_adjudications'"
        )
        assert await cur.fetchone() is None
