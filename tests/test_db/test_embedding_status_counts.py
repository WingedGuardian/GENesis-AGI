"""Tests for db.crud.memory.embedding_status_counts — the shared grouped
aggregate over memory_metadata.embedding_status used by both the dashboard
memory-health snapshot and the awareness embedding-backlog probe."""

from __future__ import annotations

import aiosqlite
import pytest

from genesis.db.crud import memory as mem_crud
from genesis.db.schema import create_all_tables


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await create_all_tables(conn)
    yield conn
    await conn.close()


async def _seed(db, status, n, *, start=0):
    rows = [
        (f"{status}-{start + i}", "2026-01-01T00:00:00", "episodic_memory", 0.5, status)
        for i in range(n)
    ]
    await db.executemany(
        "INSERT INTO memory_metadata "
        "(memory_id, created_at, collection, confidence, embedding_status) "
        "VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    await db.commit()


@pytest.mark.asyncio
async def test_empty_table_returns_empty_map(db):
    assert await mem_crud.embedding_status_counts(db) == {}


@pytest.mark.asyncio
async def test_counts_by_status(db):
    await _seed(db, "embedded", 5)
    await _seed(db, "fts5_only", 3, start=100)
    await _seed(db, "failed", 2, start=200)
    await _seed(db, "pending", 1, start=300)

    counts = await mem_crud.embedding_status_counts(db)
    assert counts == {"embedded": 5, "fts5_only": 3, "failed": 2, "pending": 1}


@pytest.mark.asyncio
async def test_absent_statuses_are_missing_not_zero(db):
    await _seed(db, "embedded", 4)
    counts = await mem_crud.embedding_status_counts(db)
    assert counts == {"embedded": 4}
    assert counts.get("failed", 0) == 0  # callers use .get()
