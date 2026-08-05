"""Database connection management for Genesis v3.

Provides async SQLite access via aiosqlite with WAL mode.
Wraps the connection in SerializedConnection to prevent concurrent
coroutines from interleaving execute+commit and locking the connection.
"""

from __future__ import annotations

import asyncio
import logging
import random
import sqlite3
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

import aiosqlite
from aiosqlite.context import Result

from genesis.env import db_busy_timeout_ms, genesis_db_path

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = genesis_db_path()

BUSY_TIMEOUT_MS = 5000

# Per-connection page cache. Negative = KiB (SQLite convention), so -262144 is
# 256 MiB. SQLite's default is ~2 MiB, which forces hot read paths to keep
# re-fetching pages instead of holding Genesis's working set resident. This is
# per *connection*, so the long-lived server + MCP connections each hold up to
# this much — a few GiB total against a 36 GiB budget, comfortably within range.
CACHE_SIZE_KIB = -262144

# Read-only recall pool (follow-up ac27b693). Recall's read stages open a
# dedicated mode=ro pool so they stop queuing behind the WHOLE server's write
# traffic on the single SerializedConnection lock. Per-connection page cache is
# deliberately MODEST here: the writer holds 256 MiB (CACHE_SIZE_KIB), but a read
# pool multiplies the cache by its size, so bound each RO connection to keep the
# pool cheap on small installs (64 MiB × a few connections stays comfortable).
RO_CACHE_SIZE_KIB = -65536  # 64 MiB per read-only connection

# Default read-pool size. Sized for the common "several concurrent CC sessions"
# workload — reads are sub-second, so a handful of parallel readers clears the
# checkout queue fast. Overridable via config; floor of 1 enforced in the pool.
DEFAULT_READ_POOL_SIZE = 4

# Schema migrations run rarely (deploy / server startup) but must win the write
# lock even when other processes (concurrent CC-session MCP servers) are writing.
# A generous timeout lets the migration's BEGIN IMMEDIATE and its COMMIT-time
# autocheckpoint wait out that contention instead of failing with
# "database is locked". The runner reconciles against schema_migrations either
# way, but this keeps the common case quiet.
MIGRATION_BUSY_TIMEOUT_MS = 60000

# Application-level lock-retry schedule (WS-1 PR-1, follow-up 2d88740d).
# "database is locked" under a multi-process write convoy is usually a LOST
# busy-wait poll race, not a long-held lock: SQLite's busy_timeout handler has no
# queue fairness, so with N processes writing in bursts one waiter can lose every
# re-poll for its whole timeout. A short jittered retry re-enters the race
# desynchronized and wins the writer slot in almost all such episodes. Delays are
# sleeps BETWEEN attempts — every attempt still gets the full PRAGMA busy_timeout
# wait inside SQLite first.
#
# ACCEPTED WORST CASE (deliberate, per-process): an EXHAUSTED episode holds the
# SerializedConnection's asyncio lock for every attempt + backoff — on the
# server's 5s default ≈ 4x5s + ~1.75s ≈ 22s with all of that process's shared-
# connection DB ops queued behind it (MCP children set 15s → ≈62s, argued at
# their setdefault site in scripts/genesis_mcp_server.py). Why acceptable: an
# exhausted episode requires ONE write to lose four consecutive full
# busy_timeout waits — i.e. a continuously-wedged writer slot for the whole
# window, a regime where every write in every process is failing regardless and
# failing this one faster helps nothing. The common convoy-loss case clears in
# the first 250ms retry and never blocks anyone longer than today's single 5s
# wait. Regression signal: the exhaustion WARN below firing in a QUIET period
# (no concurrent sessions) means a genuine long-holder, not convoy loss —
# reopen that hunt rather than tuning these delays.
_WRITE_RETRY_DELAYS: tuple[float, ...] = (0.25, 0.5, 1.0)

# ±20% jitter so waiters that lost the SAME poll race don't re-collide in
# lockstep on each retry.
_JITTER_LOW = 0.8
_JITTER_HIGH = 1.2

# Test seam: tests patch `connection._async_sleep` rather than asyncio.sleep
# (patching the shared asyncio module would affect every other coroutine in the
# test process, including pytest-asyncio's own machinery).
_async_sleep = asyncio.sleep


def _is_lock_error(exc: BaseException) -> bool:
    """True for SQLite lock errors: "database is locked" (SQLITE_BUSY) AND
    "database table is locked" (SQLITE_LOCKED), case-insensitive.

    Same predicate shape as ``db/migrations/runner.py`` — duplicated, not
    imported: this module is lower-level than the migration runner and must not
    depend on it.
    """
    return isinstance(exc, sqlite3.OperationalError) and "locked" in str(exc).lower()


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
    _OWN_ATTRS = frozenset(
        {
            "_conn",
            "_lock",
            "_reconnect_fn",
            "_consecutive_errors",
            "_max_errors",
        }
    )

    _MAX_LOCK_ERRORS = 5

    def __init__(
        self,
        conn: aiosqlite.Connection,
        *,
        reconnect_fn: Callable[[], Awaitable[aiosqlite.Connection]] | None = None,
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
                count,
                self._max_errors,
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

    async def _retry_locked(self, fn: Callable[[], Awaitable[Any]]) -> Any:
        """Run one DB call with bounded jittered retry on lock errors.

        The CALLER holds ``self._lock``, and it stays held across retries
        deliberately: releasing mid-episode would let another coroutine
        interleave its statements into this coroutine's still-open implicit
        transaction (the exact corruption this class exists to prevent).

        ``fn`` must dereference ``self._conn`` PER CALL (a lambda, never a
        captured bound method): an exhaustion-triggered reconnect in a previous
        episode swaps ``_conn``, and a captured method would pin the closed one.

        Retrying the same call is idempotent at the SQLite level (legacy
        implicit-BEGIN mode, the only mode this module uses — no
        ``isolation_level`` is ever set):
        - a locked ``execute``/``executemany`` never applied its statement;
        - a locked COMMIT leaves the transaction open (re-commit commits it) —
          or, in the WAL post-commit-autocheckpoint case, the frame is already
          durable and the connection is out of the transaction, so re-commit
          no-ops;
        - ROLLBACK likewise (and a permanently-failed ROLLBACK is the worse
          outcome: it wedges ``in_transaction=True`` until restart).
        ``executescript`` is deliberately NOT routed through this helper — a
        script can partially apply before the lock error, so re-running it is
        not idempotent.

        On exhaustion this falls through to ``_handle_lock_error``, so the
        consecutive-error counter now counts exhausted EPISODES rather than raw
        failed calls — reconnect-after-N semantics are preserved at an
        episode granularity. Only ``sqlite3.OperationalError`` lock errors are
        retried; everything else (including ``CancelledError`` during the
        sleep) propagates immediately.
        """
        attempts = len(_WRITE_RETRY_DELAYS) + 1
        slept = 0.0
        for attempt in range(1, attempts + 1):
            try:
                result = await fn()
                self._reset_error_count()
                return result
            except sqlite3.OperationalError as e:
                if not _is_lock_error(e):
                    raise
                if attempt >= attempts:
                    logger.warning(
                        "DB lock persisted through %d attempts (%.2fs of retry backoff"
                        " on top of busy_timeout) — giving up; a lock error in a QUIET"
                        " period (no concurrent sessions) points at a long-holder, not"
                        " convoy loss",
                        attempts,
                        slept,
                    )
                    await self._handle_lock_error(e)  # counts the episode; re-raises
                    raise  # unreachable (belt-and-suspenders, matches existing style)
                delay = _WRITE_RETRY_DELAYS[attempt - 1] * random.uniform(_JITTER_LOW, _JITTER_HIGH)
                slept += delay
                logger.debug(
                    "DB locked (attempt %d/%d) — retrying in %.0fms",
                    attempt,
                    attempts,
                    delay * 1000,
                )
                await _async_sleep(delay)
        msg = "unreachable: retry loop exits via return or raise"
        raise AssertionError(msg)

    # -- Operations that return Result (support both await and async with) --

    def execute(
        self,
        sql: str,
        parameters: Iterable[Any] | None = None,
    ) -> Result:
        async def _locked() -> aiosqlite.Cursor:
            async with self._lock:
                return await self._retry_locked(lambda: self._conn.execute(sql, parameters))

        return Result(_locked())

    def executemany(
        self,
        sql: str,
        parameters: Iterable[Iterable[Any]],
    ) -> Result:
        async def _locked() -> aiosqlite.Cursor:
            # Materialize BEFORE the retry loop: a generator argument would be
            # consumed by a failed first attempt, so the retry would silently
            # execute zero/partial rows and "succeed".
            params = list(parameters)
            async with self._lock:
                return await self._retry_locked(lambda: self._conn.executemany(sql, params))

        return Result(_locked())

    def execute_fetchall(
        self,
        sql: str,
        parameters: Iterable[Any] | None = None,
    ) -> Result:
        async def _locked() -> list[aiosqlite.Row]:
            async with self._lock:
                return await self._retry_locked(
                    lambda: self._conn.execute_fetchall(sql, parameters)
                )

        return Result(_locked())

    def execute_insert(
        self,
        sql: str,
        parameters: Iterable[Any] | None = None,
    ) -> Result:
        async def _locked() -> tuple | None:
            async with self._lock:
                return await self._retry_locked(lambda: self._conn.execute_insert(sql, parameters))

        return Result(_locked())

    def executescript(self, sql: str) -> Result:
        # Deliberately NOT retried (see _retry_locked docstring): a script can
        # partially apply before the lock error, so re-running it is not
        # idempotent. Keeps the pre-retry behavior: count + re-raise.
        async def _locked() -> aiosqlite.Cursor:
            async with self._lock:
                try:
                    result = await self._conn.executescript(sql)
                    self._reset_error_count()
                    return result
                except sqlite3.OperationalError as e:
                    if _is_lock_error(e):
                        await self._handle_lock_error(e)
                    raise

        return Result(_locked())

    # -- Simple async operations -------------------------------------------

    async def commit(self) -> None:
        async with self._lock:
            await self._retry_locked(lambda: self._conn.commit())

    async def rollback(self) -> None:
        # Retried like commit (idempotent in legacy isolation mode): a locked
        # ROLLBACK that never lands leaves in_transaction=True permanently —
        # the exact wedge this class exists to prevent. Previously this path
        # had NO lock handling at all.
        async with self._lock:
            await self._retry_locked(lambda: self._conn.rollback())

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


async def get_db(
    path: str | Path = DEFAULT_DB_PATH,
    *,
    foreign_keys: bool = True,
) -> SerializedConnection:
    """Open a connection to the Genesis SQLite database.

    Enables WAL mode and (by default) foreign keys.  Returns a
    SerializedConnection that prevents concurrent coroutine interleaving.
    Caller is responsible for closing.

    Set ``foreign_keys=False`` for the long-lived MCP server connections, which
    historically opened raw connections without FK enforcement — keeping FK off
    avoids surprising them with newly-enforced constraints (turning it on is a
    deliberate, separate decision).
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    async def _configure(conn: aiosqlite.Connection) -> None:
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        if foreign_keys:
            await conn.execute("PRAGMA foreign_keys=ON")
        # Env-tunable per PROCESS (GENESIS_DB_BUSY_TIMEOUT_MS): MCP children
        # raise it to ride out server-side write bursts; default 5000.
        await conn.execute(f"PRAGMA busy_timeout={db_busy_timeout_ms()}")
        await conn.execute("PRAGMA journal_size_limit=67108864")  # 64 MB WAL file cap
        # synchronous=NORMAL is the safe, standard setting under WAL (no
        # corruption risk; at most the last txn is lost on power loss). get_db
        # previously relied on the default FULL, so the hot serialized
        # connection fsynced on every commit for durability get_raw_db already
        # forgoes — align them.
        await conn.execute("PRAGMA synchronous=NORMAL")
        await conn.execute(f"PRAGMA cache_size={CACHE_SIZE_KIB}")  # 256 MiB page cache

    db = await aiosqlite.connect(str(path))
    await _configure(db)

    # Build reconnect closure (SQLite-specific; replace for PostgreSQL)
    async def _reconnect() -> aiosqlite.Connection:
        conn = await aiosqlite.connect(str(path))
        await _configure(conn)
        return conn

    return SerializedConnection(db, reconnect_fn=_reconnect)


@asynccontextmanager
async def get_raw_db(
    path: str | Path = DEFAULT_DB_PATH,
) -> AsyncIterator[aiosqlite.Connection]:
    """Open a plain aiosqlite connection with Genesis's standard pragmas.

    For short-lived, **standalone** opens — MCP fallback paths and one-shot
    reads/writes that own their own connection lifetime. Applies the same
    contention-safe pragmas every connection should have: WAL, ``synchronous=
    NORMAL`` (safe + standard with WAL), ``busy_timeout`` (so a concurrent write
    lock waits instead of failing immediately with "database is locked"), and a
    ``Row`` factory.

    Unlike :func:`get_db` this is **not** a :class:`SerializedConnection` — it
    has no cross-coroutine lock, so use it only for connections that are not
    shared across coroutines. Yields the connection and closes it on exit.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(str(path))
    try:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA synchronous=NORMAL")
        await db.execute(f"PRAGMA busy_timeout={db_busy_timeout_ms()}")
        await db.execute("PRAGMA journal_size_limit=67108864")  # 64 MB WAL file cap
        await db.execute(f"PRAGMA cache_size={CACHE_SIZE_KIB}")  # 256 MiB page cache
        # NOTE: intentionally NOT setting `foreign_keys=ON` here (get_db does).
        # These standalone sites never enforced FKs before, and none touch
        # FK-cascading tables. If a future caller needs cascade deletes, enable
        # it explicitly rather than relying on this helper.
        yield db
    finally:
        await db.close()


async def open_ro_connection(
    path: str | Path = DEFAULT_DB_PATH,
    *,
    cache_size_kib: int = RO_CACHE_SIZE_KIB,
) -> aiosqlite.Connection:
    """Open a standalone READ-ONLY aiosqlite connection (``mode=ro``, WAL-aware).

    The shared seam for zero-write readers that need the live DB without
    contending on the runtime's :class:`SerializedConnection` write lock.
    ``mode=ro`` (NOT ``immutable=1``) reads the live ``-wal``, so it sees
    committed writes — a change committed on the writer moments earlier IS
    visible here.

    Reader-safe pragmas ONLY: ``busy_timeout`` (a checkpoint's brief exclusive
    moment can otherwise throw ``SQLITE_BUSY`` at a reader), a modest
    ``cache_size``, and the ``Row`` factory for parity with :func:`get_db`.
    Deliberately does NOT run ``journal_mode=WAL`` or ``synchronous`` — those
    need write access to the DB header and are no-ops (or raise
    ``SQLITE_READONLY`` on some builds) on a read-only handle.
    """
    uri = f"file:{Path(path)}?mode=ro"
    conn = await aiosqlite.connect(uri, uri=True)
    conn.row_factory = aiosqlite.Row
    await conn.execute(f"PRAGMA busy_timeout={db_busy_timeout_ms()}")
    await conn.execute(f"PRAGMA cache_size={cache_size_kib}")
    return conn


class ReadPoolClosed(Exception):
    """Raised by :meth:`ReadConnectionPool.acquire` when the pool is closed or
    was never opened. Callers treat it as "use the shared connection instead."
    """


class ReadConnectionPool:
    """Small fixed pool of read-only aiosqlite connections for hot read paths.

    Each connection is a ``mode=ro`` handle (:func:`open_ro_connection`). WAL
    allows unlimited concurrent readers, so N connections give N genuinely-
    parallel readers that never queue behind the ``SerializedConnection`` write
    lock — the fix for recall reads stalling behind the whole server's writes
    under concurrent sessions (follow-up ac27b693).

    Checkout is **exclusive** (an ``asyncio.Queue``): ``async with
    pool.acquire() as conn`` hands ONE connection to ONE coroutine at a time,
    which is exactly why the pooled connections need no per-connection lock (a
    raw aiosqlite connection is safe for a single owner; the ``SerializedConnection``
    lock only exists because many coroutines share one connection). The
    (size+1)th concurrent reader blocks on the queue until a slot frees —
    natural backpressure; the route's own timeout is the ultimate bound.

    A pooled ``mode=ro`` autocommit ``SELECT`` never opens a transaction, so a
    read that errors or is cancelled leaves no dangling state — the connection
    is always safe to return to the pool, so there is deliberately no
    replace-on-error logic (which would ``await`` in a ``finally`` under route-
    timeout cancellation).

    The pool is an OPTIMIZATION, never a hard dependency: callers fall back to
    the shared write connection on any pool miss/error, so it can never make a
    read WORSE than the pre-pool behavior.
    """

    def __init__(
        self,
        path: str | Path = DEFAULT_DB_PATH,
        *,
        size: int = DEFAULT_READ_POOL_SIZE,
        cache_size_kib: int = RO_CACHE_SIZE_KIB,
    ) -> None:
        self._path = str(Path(path))
        self._size = max(1, size)
        self._cache_size_kib = cache_size_kib
        self._queue: asyncio.Queue[aiosqlite.Connection] = asyncio.Queue()
        self._all: list[aiosqlite.Connection] = []
        self._closed = False
        self._opened = False

    @property
    def size(self) -> int:
        return self._size

    async def open(self) -> None:
        """Open all connections and fill the checkout queue. Idempotent."""
        if self._opened:
            return
        for _ in range(self._size):
            conn = await open_ro_connection(self._path, cache_size_kib=self._cache_size_kib)
            self._all.append(conn)
            self._queue.put_nowait(conn)
        self._opened = True

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[aiosqlite.Connection]:
        """Check out one connection for the duration of the ``async with`` block.

        Raises :class:`ReadPoolClosed` if the pool is closed or not yet opened
        (the caller falls back to the shared connection). The connection is
        returned to the queue on exit — including on error/cancellation, which
        is safe for autocommit ``mode=ro`` reads (no dangling transaction). If
        the pool was closed while the connection was held, it is dropped rather
        than returned (``close()`` closes every ``self._all`` connection).
        """
        if self._closed or not self._opened:
            raise ReadPoolClosed
        conn = await self._queue.get()
        try:
            yield conn
        finally:
            # No ``await`` here: put_nowait can't fail (unbounded queue), so the
            # slot is returned even under route-timeout cancellation. On a
            # close() race we drop the handle — close() owns closing self._all.
            if not self._closed:
                self._queue.put_nowait(conn)

    async def close(self) -> None:
        """Close every connection. Idempotent; safe to call at shutdown."""
        if self._closed:
            return
        self._closed = True
        for conn in self._all:
            with suppress(Exception):
                await conn.close()
        self._all.clear()


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
