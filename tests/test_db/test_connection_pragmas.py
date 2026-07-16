"""Tests for WAL-bounding pragmas + the (now async) WAL checkpoint helpers.

journal_size_limit caps the WAL file in normal operation; the checkpoint helpers
must use the async aiosqlite API (the old sync ``db._conn._conn.execute`` path
raised a thread-bound ProgrammingError that a bare except swallowed → no-op).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from genesis.awareness import loop
from genesis.db.connection import get_db, get_raw_db

_LIMIT = 67108864  # 64 MB
_CACHE_SIZE = -262144  # 256 MiB page cache (negative = KiB), matches CACHE_SIZE_KIB


@pytest.mark.asyncio
async def test_get_db_sets_journal_size_limit(tmp_path):
    db = await get_db(tmp_path / "g.db")
    try:
        rows = await db.execute_fetchall("PRAGMA journal_size_limit")
        assert rows[0][0] == _LIMIT
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_get_raw_db_sets_journal_size_limit(tmp_path):
    async with get_raw_db(tmp_path / "g.db") as db:
        rows = await db.execute_fetchall("PRAGMA journal_size_limit")
        assert rows[0][0] == _LIMIT


@pytest.mark.asyncio
async def test_get_db_sets_cache_size(tmp_path):
    db = await get_db(tmp_path / "g.db")
    try:
        rows = await db.execute_fetchall("PRAGMA cache_size")
        assert rows[0][0] == _CACHE_SIZE
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_get_raw_db_sets_cache_size(tmp_path):
    async with get_raw_db(tmp_path / "g.db") as db:
        rows = await db.execute_fetchall("PRAGMA cache_size")
        assert rows[0][0] == _CACHE_SIZE


@pytest.mark.asyncio
async def test_get_db_sets_synchronous_normal(tmp_path):
    """get_db previously relied on the SQLite default FULL(2); it now aligns to
    NORMAL(1) like get_raw_db — safe + standard under WAL, fewer fsyncs."""
    db = await get_db(tmp_path / "g.db")
    try:
        rows = await db.execute_fetchall("PRAGMA synchronous")
        assert rows[0][0] == 1  # 1 == NORMAL
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_wal_checkpoint_helpers_actually_run(tmp_path):
    """Regression: the helpers use the async API, so a TRUNCATE actually reclaims
    the WAL file (the old main-thread raw-cursor path was a silent no-op)."""
    db_path = tmp_path / "g.db"
    db = await get_db(db_path)
    try:
        await db.execute("CREATE TABLE t (x INTEGER)")
        for i in range(200):
            await db.execute("INSERT INTO t VALUES (?)", (i,))
        await db.commit()

        wal = Path(f"{db_path}-wal")
        # PASSIVE must not raise; TRUNCATE should zero the WAL file (sole reader).
        await loop._sqlite_wal_checkpoint(db)
        await loop._sqlite_wal_truncate(db)

        assert (not wal.exists()) or wal.stat().st_size == 0
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_pragmas_survive_reconnect(tmp_path):
    """The reconnect closure reuses _configure, so a recovered connection must
    keep cache_size + synchronous — guards against a future refactor inlining the
    pragmas and dropping them on the reconnect path."""
    db = await get_db(tmp_path / "g.db")
    try:
        orig_conn = db._conn

        async def _raise_locked(*args, **kwargs):
            raise sqlite3.OperationalError("database is locked")

        # Force lock errors until the SerializedConnection trips its reconnect
        # threshold and swaps in a fresh connection built by the REAL closure.
        orig_conn.execute = _raise_locked
        for _ in range(db._MAX_LOCK_ERRORS):
            with pytest.raises(sqlite3.OperationalError):
                await db.execute("SELECT 1")

        assert db._conn is not orig_conn  # actually reconnected
        rows = await db.execute_fetchall("PRAGMA cache_size")
        assert rows[0][0] == _CACHE_SIZE
        rows = await db.execute_fetchall("PRAGMA synchronous")
        assert rows[0][0] == 1  # NORMAL
    finally:
        await db.close()
