"""Snapshot test: memory_health() surfaces the embedding_backlog key with
failed/pending counts drawn from memory_metadata (the durable mirror), which
feeds the neural-monitor dashboard's always-on backlog display."""

from __future__ import annotations

import aiosqlite
import pytest

from genesis.db.schema import create_all_tables
from genesis.observability.snapshots.memory_health import memory_health


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
async def test_embedding_backlog_key_present_and_counts(db):
    await _seed(db, "embedded", 10)
    await _seed(db, "failed", 4, start=100)
    await _seed(db, "pending", 2, start=200)
    await _seed(db, "fts5_only", 7, start=300)  # healthy — must NOT count

    snap = await memory_health(db)
    assert "embedding_backlog" in snap
    assert snap["embedding_backlog"] == {"failed": 4, "pending": 2}


@pytest.mark.asyncio
async def test_embedding_backlog_zero_baseline(db):
    await _seed(db, "embedded", 5)
    snap = await memory_health(db)
    assert snap["embedding_backlog"] == {"failed": 0, "pending": 0}


@pytest.mark.asyncio
async def test_no_db_unavailable_has_no_backlog_key(db):
    snap = await memory_health(None)
    assert snap == {"status": "unavailable"}
