"""Migration 0049 — add idx_dead_letter_created.

Verifies the index is created on a pre-existing dead_letter table (the
existing-DB upgrade path), the add is idempotent, and a missing table is a
safe no-op (the runner's lifecycle tests apply migrations to a bare DB).
Mirrors 0048.
"""

from __future__ import annotations

import importlib

import aiosqlite
import pytest

M49 = importlib.import_module(
    "genesis.db.migrations.0049_dead_letter_created_index"
)

_DDL = """
    CREATE TABLE dead_letter (
        id TEXT PRIMARY KEY, operation_type TEXT NOT NULL, payload TEXT NOT NULL,
        target_provider TEXT NOT NULL, failure_reason TEXT NOT NULL,
        created_at TEXT NOT NULL, retry_count INTEGER DEFAULT 0,
        last_retry_at TEXT, status TEXT DEFAULT 'pending'
    )
"""


async def _indexes(db: aiosqlite.Connection) -> set[str]:
    cur = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='dead_letter'"
    )
    return {row[0] for row in await cur.fetchall()}


@pytest.mark.asyncio
async def test_up_adds_index_to_existing_table(tmp_path):
    async with aiosqlite.connect(str(tmp_path / "t.db")) as db:
        await db.execute(_DDL)
        assert "idx_dead_letter_created" not in await _indexes(db)
        await M49.up(db)
        assert "idx_dead_letter_created" in await _indexes(db)


@pytest.mark.asyncio
async def test_up_is_idempotent(tmp_path):
    async with aiosqlite.connect(str(tmp_path / "t.db")) as db:
        await db.execute(_DDL)
        await M49.up(db)
        await M49.up(db)  # second run must not raise
        assert "idx_dead_letter_created" in await _indexes(db)


@pytest.mark.asyncio
async def test_up_noop_when_table_absent(tmp_path):
    async with aiosqlite.connect(str(tmp_path / "t.db")) as db:
        await M49.up(db)  # no dead_letter table — safe no-op
        cur = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='dead_letter'"
        )
        assert await cur.fetchone() is None
