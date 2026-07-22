"""HybridRetriever._ro_read seam (follow-up ac27b693, PR-4).

recall's pure reads route through ``_ro_read``, which runs them on a pooled
``mode=ro`` connection when a pool is wired and falls back to the shared write
connection (``self._db``) on any pool miss/error. That fallback is the safety
net that makes the pool optional: a read is never WORSE than the pre-pool path,
and a security-relevant read (the WS-3 origin backfill) that hits a transient
pool error re-reads real data instead of fail-open ``None``.
"""

from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

from genesis.db.connection import ReadConnectionPool, ReadPoolClosed
from genesis.memory.proactive import _breadcrumbs, _enrich
from genesis.memory.retrieval import HybridRetriever


def _retriever(read_pool=None) -> HybridRetriever:
    embed = MagicMock()
    embed.embed = AsyncMock(return_value=[0.1] * 1024)
    return HybridRetriever(
        embedding_provider=embed,
        qdrant_client=MagicMock(),
        db=MagicMock(name="shared_db"),
        read_pool=read_pool,
    )


class _FakePool:
    """Minimal pool: ``acquire()`` yields a fixed connection, or raises."""

    def __init__(self, conn, *, raise_exc=None):
        self._conn = conn
        self._raise_exc = raise_exc
        self.acquired = 0

    @asynccontextmanager
    async def acquire(self):
        if self._raise_exc is not None:
            raise self._raise_exc
        self.acquired += 1
        yield self._conn


async def test_ro_read_uses_pool_when_present():
    ro_conn = object()
    pool = _FakePool(ro_conn)
    retriever = _retriever(read_pool=pool)

    async def fn(conn, x):
        assert conn is ro_conn  # ran on the pooled RO connection, not self._db
        return x * 2

    assert await retriever._ro_read(fn, 5) == 10
    assert pool.acquired == 1


async def test_ro_read_falls_back_to_db_without_pool():
    retriever = _retriever(read_pool=None)
    seen: dict = {}

    async def fn(conn, x):
        seen["conn"] = conn
        return x

    assert await retriever._ro_read(fn, 7) == 7
    assert seen["conn"] is retriever._db  # byte-identical to the pre-pool path


async def test_ro_read_falls_back_to_db_on_pool_error():
    """A pool acquire/read error re-runs on self._db — the safety net that makes
    the pool optional. Also the WS-3 origin-backfill security path: a transient
    pool error re-reads true data on the shared connection, never fail-open None.
    """
    pool = _FakePool(object(), raise_exc=RuntimeError("pool down"))
    retriever = _retriever(read_pool=pool)
    seen: dict = {}

    async def fn(conn, x):
        seen["conn"] = conn
        return x

    assert await retriever._ro_read(fn, 3) == 3
    assert seen["conn"] is retriever._db


async def test_ro_read_falls_back_on_readpoolclosed():
    pool = _FakePool(object(), raise_exc=ReadPoolClosed())
    retriever = _retriever(read_pool=pool)
    seen: dict = {}

    async def fn(conn, x):
        seen["conn"] = conn
        return x

    assert await retriever._ro_read(fn, 1) == 1
    assert seen["conn"] is retriever._db


async def test_enrich_and_breadcrumbs_run_through_pool(tmp_path):
    """PR-4b functional: the proactive engine's post-recall reads (``_enrich`` +
    ``_breadcrumbs``) execute correctly on a POOLED ``mode=ro`` connection via
    ``_ro_read`` — backfilling ``_created_at``/``_wing`` and attaching
    ``related_ids`` off the shared write lock, exactly as the engine now calls
    them. Both queries are indexed seeks; this pins that they read real rows
    through the pool, not just that ``_ro_read`` forwards a connection.
    """
    db_path = tmp_path / "engine.db"
    con = sqlite3.connect(str(db_path))
    con.executescript(
        "CREATE TABLE memory_metadata (memory_id TEXT PRIMARY KEY, created_at TEXT, wing TEXT);"
        "CREATE TABLE memory_links (source_id TEXT, target_id TEXT, strength REAL);"
    )
    con.execute(
        "INSERT INTO memory_metadata VALUES ('mem1', '2024-01-01T00:00:00+00:00', 'routing')"
    )
    con.executemany(
        "INSERT INTO memory_links (source_id, target_id, strength) VALUES (?, ?, ?)",
        [("mem1", "aaaaaaaa01", 0.9), ("mem1", "bbbbbbbb02", 0.7), ("mem1", "cccccccc03", 0.3)],
    )
    con.commit()
    con.close()

    pool = ReadConnectionPool(db_path, size=2)
    await pool.open()
    retriever = _retriever(read_pool=pool)
    try:
        dicts = [{"memory_id": "mem1", "payload": {}}]

        await retriever._ro_read(_enrich, dicts)
        assert dicts[0]["_created_at"] == "2024-01-01T00:00:00+00:00"
        assert dicts[0]["_wing"] == "routing"  # payload wing empty → metadata wing

        await retriever._ro_read(_breadcrumbs, dicts)
        # top-2 neighbors with strength >= 0.5, DESC; the 0.3 link is excluded.
        assert dicts[0]["related_ids"] == ["aaaaaaaa", "bbbbbbbb"]
    finally:
        await pool.close()
