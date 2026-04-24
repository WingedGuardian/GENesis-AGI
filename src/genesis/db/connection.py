"""Database connection management for Genesis v3.

Provides async SQLite access via aiosqlite with WAL mode.
Wraps the connection in SerializedConnection to prevent concurrent
coroutines from interleaving execute+commit and locking the connection.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import aiosqlite
from aiosqlite.context import Result

from genesis.env import genesis_db_path

DEFAULT_DB_PATH = genesis_db_path()

BUSY_TIMEOUT_MS = 5000


class SerializedConnection:
    """Proxy that serializes all DB operations through an asyncio.Lock.

    Genesis shares a single aiosqlite.Connection across all subsystems.
    Without serialization, concurrent coroutines can simultaneously call
    execute/commit on the underlying connection, corrupting the aiosqlite
    thread's transaction state and leaving in_transaction=True permanently
    (requiring a server restart).

    The lock ensures only one coroutine touches the underlying connection
    at a time.  Each method acquires and releases the lock independently,
    so two coroutines doing ``execute(); commit()`` may interleave at the
    method boundary (A.execute → B.execute → A.commit → B.commit).  This
    is safe: both operations execute serially on aiosqlite's background
    thread, and commit() flushes all pending work.  The lock prevents the
    actual failure mode — simultaneous access to the connection.

    Reads are serialized behind the same lock as writes.  SQLite
    operations are sub-millisecond (~1.8ms for write+commit), so the
    overhead is negligible.  A read-write lock could be used if read
    contention becomes measurable.

    execute/executemany/execute_fetchall/execute_insert/executescript
    return aiosqlite.context.Result objects (not coroutines) so that
    both ``await db.execute(...)`` and ``async with db.execute(...) as cur:``
    patterns continue to work transparently.
    """

    # Attributes that live on the proxy itself, not the wrapped connection.
    _OWN_ATTRS = frozenset({"_conn", "_lock"})

    def __init__(self, conn: aiosqlite.Connection) -> None:
        object.__setattr__(self, "_conn", conn)
        object.__setattr__(self, "_lock", asyncio.Lock())

    # -- Attribute passthrough (e.g. row_factory, in_transaction) ----------

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name in self._OWN_ATTRS:
            object.__setattr__(self, name, value)
        else:
            setattr(self._conn, name, value)

    # -- Operations that return Result (support both await and async with) --

    def execute(
        self, sql: str, parameters: Iterable[Any] | None = None,
    ) -> Result:
        async def _locked() -> aiosqlite.Cursor:
            async with self._lock:
                return await self._conn.execute(sql, parameters)
        return Result(_locked())

    def executemany(
        self, sql: str, parameters: Iterable[Iterable[Any]],
    ) -> Result:
        async def _locked() -> aiosqlite.Cursor:
            async with self._lock:
                return await self._conn.executemany(sql, parameters)
        return Result(_locked())

    def execute_fetchall(
        self, sql: str, parameters: Iterable[Any] | None = None,
    ) -> Result:
        async def _locked() -> list[aiosqlite.Row]:
            async with self._lock:
                return await self._conn.execute_fetchall(sql, parameters)
        return Result(_locked())

    def execute_insert(
        self, sql: str, parameters: Iterable[Any] | None = None,
    ) -> Result:
        async def _locked() -> tuple | None:
            async with self._lock:
                return await self._conn.execute_insert(sql, parameters)
        return Result(_locked())

    def executescript(self, sql: str) -> Result:
        async def _locked() -> aiosqlite.Cursor:
            async with self._lock:
                return await self._conn.executescript(sql)
        return Result(_locked())

    # -- Simple async operations -------------------------------------------

    async def commit(self) -> None:
        async with self._lock:
            await self._conn.commit()

    async def rollback(self) -> None:
        async with self._lock:
            await self._conn.rollback()

    async def close(self) -> None:
        async with self._lock:
            await self._conn.close()

    async def cursor(self) -> aiosqlite.Cursor:
        async with self._lock:
            return await self._conn.cursor()

    # -- Async iteration support (used by some callers) --------------------

    async def __aenter__(self) -> SerializedConnection:
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        pass


async def get_db(path: str | Path = DEFAULT_DB_PATH) -> SerializedConnection:
    """Open a connection to the Genesis SQLite database.

    Enables WAL mode and foreign keys.  Returns a SerializedConnection
    that prevents concurrent coroutine interleaving.
    Caller is responsible for closing.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    db = await aiosqlite.connect(str(path))
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    await db.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    return SerializedConnection(db)


async def init_db(path: str | Path = DEFAULT_DB_PATH) -> SerializedConnection:
    """Initialize the database: create all tables, indexes, and seed data.

    Returns the open SerializedConnection.
    """
    from genesis.db.schema import create_all_tables, seed_data

    db = await get_db(path)
    await create_all_tables(db)
    await seed_data(db)
    await db.commit()
    return db
