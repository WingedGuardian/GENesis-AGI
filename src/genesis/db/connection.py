"""Database connection management for Genesis v3.

Provides async SQLite access via aiosqlite with WAL mode.
Wraps the connection in SerializedConnection to prevent concurrent
coroutines from interleaving execute+commit and locking the connection.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

import aiosqlite
from aiosqlite.context import Result

from genesis.env import genesis_db_path

logger = logging.getLogger(__name__)

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
    _OWN_ATTRS = frozenset({
        "_conn", "_lock", "_reconnect_fn",
        "_consecutive_errors", "_max_errors",
    })

    _MAX_LOCK_ERRORS = 5

    def __init__(
        self,
        conn: aiosqlite.Connection,
        *,
        reconnect_fn: Callable[[], aiosqlite.Connection] | None = None,
    ) -> None:
        object.__setattr__(self, "_conn", conn)
        object.__setattr__(self, "_lock", asyncio.Lock())
        object.__setattr__(self, "_reconnect_fn", reconnect_fn)
        object.__setattr__(self, "_consecutive_errors", 0)
        object.__setattr__(self, "_max_errors", self._MAX_LOCK_ERRORS)

    # -- Attribute passthrough (e.g. row_factory, in_transaction) ----------

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name in self._OWN_ATTRS:
            object.__setattr__(self, name, value)
        else:
            setattr(self._conn, name, value)

    # -- Error tracking and recovery ----------------------------------------

    def _reset_error_count(self) -> None:
        object.__setattr__(self, "_consecutive_errors", 0)

    async def _handle_lock_error(self, exc: Exception) -> None:
        """Track lock errors and attempt reconnection after threshold."""
        count = self._consecutive_errors + 1
        object.__setattr__(self, "_consecutive_errors", count)
        if count >= self._max_errors and self._reconnect_fn is not None:
            logger.warning(
                "DB lock error %d/%d — attempting reconnection",
                count, self._max_errors,
            )
            try:
                old_conn = self._conn
                try:
                    await old_conn.close()
                except Exception:
                    logger.debug("Old connection close failed", exc_info=True)
                new_conn = await self._reconnect_fn()
                object.__setattr__(self, "_conn", new_conn)
                object.__setattr__(self, "_consecutive_errors", 0)
                logger.info("DB connection recovered after %d lock errors", count)
            except Exception:
                logger.error("DB reconnection failed", exc_info=True)
        raise exc

    # -- Operations that return Result (support both await and async with) --

    def execute(
        self, sql: str, parameters: Iterable[Any] | None = None,
    ) -> Result:
        async def _locked() -> aiosqlite.Cursor:
            async with self._lock:
                try:
                    result = await self._conn.execute(sql, parameters)
                    self._reset_error_count()
                    return result
                except sqlite3.OperationalError as e:
                    if "locked" in str(e):
                        await self._handle_lock_error(e)
                    raise
        return Result(_locked())

    def executemany(
        self, sql: str, parameters: Iterable[Iterable[Any]],
    ) -> Result:
        async def _locked() -> aiosqlite.Cursor:
            async with self._lock:
                try:
                    result = await self._conn.executemany(sql, parameters)
                    self._reset_error_count()
                    return result
                except sqlite3.OperationalError as e:
                    if "locked" in str(e):
                        await self._handle_lock_error(e)
                    raise
        return Result(_locked())

    def execute_fetchall(
        self, sql: str, parameters: Iterable[Any] | None = None,
    ) -> Result:
        async def _locked() -> list[aiosqlite.Row]:
            async with self._lock:
                try:
                    result = await self._conn.execute_fetchall(sql, parameters)
                    self._reset_error_count()
                    return result
                except sqlite3.OperationalError as e:
                    if "locked" in str(e):
                        await self._handle_lock_error(e)
                    raise
        return Result(_locked())

    def execute_insert(
        self, sql: str, parameters: Iterable[Any] | None = None,
    ) -> Result:
        async def _locked() -> tuple | None:
            async with self._lock:
                try:
                    result = await self._conn.execute_insert(sql, parameters)
                    self._reset_error_count()
                    return result
                except sqlite3.OperationalError as e:
                    if "locked" in str(e):
                        await self._handle_lock_error(e)
                    raise
        return Result(_locked())

    def executescript(self, sql: str) -> Result:
        async def _locked() -> aiosqlite.Cursor:
            async with self._lock:
                try:
                    result = await self._conn.executescript(sql)
                    self._reset_error_count()
                    return result
                except sqlite3.OperationalError as e:
                    if "locked" in str(e):
                        await self._handle_lock_error(e)
                    raise
        return Result(_locked())

    # -- Simple async operations -------------------------------------------

    async def commit(self) -> None:
        async with self._lock:
            try:
                await self._conn.commit()
                self._reset_error_count()
            except sqlite3.OperationalError as e:
                if "locked" in str(e):
                    await self._handle_lock_error(e)
                raise

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

    # Build reconnect closure (SQLite-specific; replace for PostgreSQL)
    async def _reconnect() -> aiosqlite.Connection:
        conn = await aiosqlite.connect(str(path))
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA foreign_keys=ON")
        await conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        return conn

    return SerializedConnection(db, reconnect_fn=_reconnect)


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
