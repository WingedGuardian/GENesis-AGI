"""Migration 0047 — create build_candidates (capability-build lane ledger).

Verifies table + index creation on an empty DB, idempotency (fresh DBs already
have the table from _tables.py), the CHECK constraints, and the partial unique
index (one OPEN candidate per item_key). Mirrors the 0044 new-table pattern.
"""

from __future__ import annotations

import importlib
import sqlite3

import aiosqlite
import pytest

M47 = importlib.import_module("genesis.db.migrations.0047_build_candidates")


async def _tables(db: aiosqlite.Connection) -> set[str]:
    cur = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    )
    return {row[0] for row in await cur.fetchall()}


async def _indexes(db: aiosqlite.Connection) -> set[str]:
    cur = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'"
    )
    return {row[0] for row in await cur.fetchall()}


@pytest.mark.asyncio
async def test_up_creates_table_and_indexes(tmp_path):
    async with aiosqlite.connect(str(tmp_path / "t.db")) as db:
        await M47.up(db)
        assert "build_candidates" in await _tables(db)
        idx = await _indexes(db)
        assert "idx_build_candidates_open_item" in idx
        assert "idx_build_candidates_outcome" in idx
        assert "idx_build_candidates_created" in idx


@pytest.mark.asyncio
async def test_up_is_idempotent(tmp_path):
    async with aiosqlite.connect(str(tmp_path / "t.db")) as db:
        await M47.up(db)
        await M47.up(db)  # second run must NOT raise
        assert "build_candidates" in await _tables(db)


@pytest.mark.asyncio
async def test_verdict_check_constraint(tmp_path):
    async with aiosqlite.connect(str(tmp_path / "t.db")) as db:
        await M47.up(db)
        with pytest.raises(sqlite3.IntegrityError):
            await db.execute(
                "INSERT INTO build_candidates "
                "(id, item_key, item_title, source_file, verdict) "
                "VALUES ('c1', 'k', 'title', 'n.md', 'maybe')"
            )


@pytest.mark.asyncio
async def test_partial_unique_open_candidate(tmp_path):
    async with aiosqlite.connect(str(tmp_path / "t.db")) as db:
        await M47.up(db)
        await db.execute(
            "INSERT INTO build_candidates "
            "(id, item_key, item_title, source_file, verdict) "
            "VALUES ('c1', 'k', 'title', 'n.md', 'build')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            await db.execute(
                "INSERT INTO build_candidates "
                "(id, item_key, item_title, source_file, verdict) "
                "VALUES ('c2', 'k', 'title', 'n.md', 'build')"
            )
        await db.execute(
            "UPDATE build_candidates SET user_decision = 'approved' WHERE id = 'c1'"
        )
        # Decided row no longer blocks a new open candidate.
        await db.execute(
            "INSERT INTO build_candidates "
            "(id, item_key, item_title, source_file, verdict) "
            "VALUES ('c3', 'k', 'title', 'n.md', 'build')"
        )


@pytest.mark.asyncio
async def test_down_drops_table(tmp_path):
    async with aiosqlite.connect(str(tmp_path / "t.db")) as db:
        await M47.up(db)
        await M47.down(db)
        assert "build_candidates" not in await _tables(db)
