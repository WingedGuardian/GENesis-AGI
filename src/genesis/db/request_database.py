"""Request-owned SQLite connections for opt-in shared MCP callers.

The facade is long lived; its physical connections are not. Existing services
can retain it without retaining another request's transaction. Explicit CRUD
commits keep their meaning: this is isolation, not whole-tool atomicity.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anyio

from genesis.db.connection import (
    DEFAULT_CHECKOUT_TIMEOUT_S,
    ConnectionCleanupError,
    SerializedConnection,
    _CursorResult,
    drain_connection_worker,
    get_db,
)

logger = logging.getLogger(__name__)


class DatabaseScopeError(RuntimeError):
    """SQL was attempted without a live owning unit of work."""


class BackgroundTaskCapacityError(DatabaseScopeError):
    """The bounded background job budget refused work before acceptance."""


@dataclass(eq=False)
class _Lease:
    connection: SerializedConnection | None = None
    active: bool = True
    closed: bool = False
    cursors: set[Any] = field(default_factory=set)

    def require_active(self) -> SerializedConnection:
        if not self.active or self.connection is None:
            raise DatabaseScopeError("Database unit of work has ended")
        return self.connection


class _Cursor:
    def __init__(self, cursor: Any, lease: _Lease):
        self._cursor = cursor
        self._lease = lease
        lease.cursors.add(self)

    def __getattr__(self, name: str) -> Any:
        self._lease.require_active()
        if name == "connection":
            raise DatabaseScopeError("Raw cursor connections are not exposed by scoped units")
        value = getattr(self._cursor, name)
        # Callable properties (notably sqlite3.Row) are values, not methods.
        if name in {"fetchone", "fetchmany", "fetchall", "execute", "executemany", "executescript"}:
            async def checked(*args, **kwargs):
                self._lease.require_active()
                result = await value(*args, **kwargs)
                # _TimedCursor delegates fluent operations to its native cursor;
                # that result is not identical to the timed wrapper we retain.
                return self if name in {"execute", "executemany", "executescript"} else result
            return checked
        return value

    def __setattr__(self, name: str, value: Any) -> None:
        if name.startswith("_"):
            object.__setattr__(self, name, value)
        else:
            self._lease.require_active()
            setattr(self._cursor, name, value)

    async def close(self) -> None:
        await self._cursor.close()
        self._lease.cursors.discard(self)

    async def __aenter__(self):
        self._lease.require_active()
        return self

    async def __aexit__(self, *exc):
        await self.close()

    async def __aiter__(self):
        while rows := await self.fetchmany(self._cursor.iter_chunk_size):
            for row in rows:
                self._lease.require_active()
                yield row


async def _settle(task: asyncio.Task) -> Any:
    """Finish owned cleanup despite level cancellation and repeated cancel()."""
    interrupted = None
    with anyio.CancelScope(shield=True):
        while True:
            try:
                result = await asyncio.shield(task)
                break
            except asyncio.CancelledError as exc:
                if task.cancelled():
                    raise
                interrupted = exc
    if interrupted is not None:
        raise interrupted
    return result


class RequestDatabase:
    """Stable DB facade; every independent scope gets a fresh connection.

    ``max_units`` bounds foreground opening, active and closing leases. Explicit
    background budgets add a separate pool; total leases cannot exceed their sum.
    Independently owned connections in existing tool implementations are not
    included. There is no fallback to an unrelated connection on exhaustion.
    """

    request_scoped = True

    def __init__(
        self, path: str | Path, *, max_units: int,
        checkout_timeout_s: float = DEFAULT_CHECKOUT_TIMEOUT_S,
        max_background_units: int | None = None,
        max_background_jobs: int | None = None,
    ):
        if (max_background_units is None) != (max_background_jobs is None):
            raise ValueError("both background budgets must be configured together")
        limits = {"max_units": max_units}
        if max_background_units is not None:
            limits.update(max_background_units=max_background_units, max_background_jobs=max_background_jobs)
        for name, value in limits.items():
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if not math.isfinite(checkout_timeout_s) or checkout_timeout_s <= 0:
            raise ValueError("checkout_timeout_s must be finite and positive")
        self.path = Path(path).expanduser().resolve()
        self._foreground_limit = max_units
        self._slots = asyncio.BoundedSemaphore(max_units)
        self._background_slots = (
            asyncio.BoundedSemaphore(max_background_units) if max_background_units is not None else None
        )
        self._background_job_limit = max_background_jobs or 0
        self._background_job_count = 0
        self._background_scope: ContextVar[bool] = ContextVar("database_background_scope", default=False)
        self._timeout = checkout_timeout_s
        self._current: ContextVar[_Lease | None] = ContextVar("database_lease", default=None)
        self._owners: dict[asyncio.Task, asyncio.Future[None]] = {}
        self._workers: set[asyncio.Task] = set()
        self._jobs: set[asyncio.Task] = set()
        self._closing = False
        self._close_task: asyncio.Task | None = None
        self._unclosed: set[_Lease] = set()

    @property
    def foreground_limit(self) -> int:
        """Configured foreground capacity; live checkout count is separate."""
        return self._foreground_limit

    def _lease(self) -> _Lease:
        lease = self._current.get()
        if lease is None:
            raise DatabaseScopeError("Database operation requires unit_of_work()")
        lease.require_active()
        return lease

    def _worker(self, coroutine) -> asyncio.Task:
        task = asyncio.create_task(coroutine)
        self._workers.add(task)
        task.add_done_callback(self._workers.discard)
        return task

    async def _open(self, lease: _Lease) -> None:
        lease.connection = await get_db(
            self.path, foreign_keys=False, existing_only=True, reconnect=False,
        )

    async def _finish(self, opening: asyncio.Task, lease: _Lease, success: bool) -> None:
        try:
            await opening
        except ConnectionCleanupError:
            # No confirmed close: retain admission and fail shutdown too.
            raise
        except BaseException:
            # The factory closes partially configured connections. No usable
            # connection was handed to a tool; preserve the original error.
            lease.closed = True
            return
        conn = lease.connection
        if conn is None:
            raise DatabaseScopeError("Opening completed without a connection")
        try:
            for cursor in tuple(lease.cursors):
                await cursor.close()
            if success:
                await conn.commit()
            else:
                await conn.rollback()
        finally:
            # close queues behind SQL already executing on aiosqlite's worker.
            # Never cancel this task or give its slot to another caller early.
            await conn.close()
            await drain_connection_worker(conn._conn)
            lease.closed = True

    @asynccontextmanager
    async def unit_of_work(self, *, independent: bool = False) -> AsyncIterator[RequestDatabase]:
        existing = self._current.get()
        if existing is not None and not independent:
            existing.require_active()
            yield self
            return
        if self._closing:
            raise DatabaseScopeError("Database manager is closing")
        owner = asyncio.current_task()
        if owner is None:
            raise DatabaseScopeError("A unit of work requires an asyncio task")
        if owner in self._owners:
            raise DatabaseScopeError("Independent work must run in a separate task")
        background = self._background_scope.get()
        slots = self._background_slots if background else self._slots
        if slots is None:
            raise ValueError("background admission is not configured")
        finished: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._owners[owner] = finished
        acquired = False
        opening = None
        token = None
        lease = _Lease()
        success = False
        try:
            if background:
                # Accepted jobs are already bounded by the scheduling budget.
                # Queue until a background connection is free or shutdown cancels.
                await slots.acquire()
            else:
                async with asyncio.timeout(self._timeout):
                    await slots.acquire()
            acquired = True
            opening = self._worker(self._open(lease))
            await asyncio.shield(opening)
            token = self._current.set(lease)
            yield self
            success = True
        finally:
            lease.active = False
            if token is not None:
                try:
                    self._current.reset(token)
                except ValueError:
                    # Left from another task or a finalizer. Cleanup must still run.
                    logger.warning("Database unit of work ended outside its owning context")
            try:
                if opening is not None:
                    cleanup = self._worker(self._finish(opening, lease, success))
                    await _settle(cleanup)
            finally:
                if acquired:
                    if lease.closed:
                        slots.release()
                    else:
                        self._unclosed.add(lease)
                        logger.error("Database connection closure could not be confirmed")
                del self._owners[owner]
                finished.set_result(None)

    def spawn(self, coroutine, **task_options) -> asyncio.Task:
        """Schedule independent work, preserving all non-database context.

        Ownership starts synchronously, including jobs cancelled before their
        coroutine enters or while waiting for admission. Shutdown joins them.
        """
        from genesis.util.tasks import tracked_task

        if self._closing:
            coroutine.close()
            raise DatabaseScopeError("Database manager is closing")
        if self._background_slots is None:
            coroutine.close()
            raise ValueError("detached work requires explicit background budgets")
        if self._background_job_count >= self._background_job_limit:
            coroutine.close()
            raise BackgroundTaskCapacityError("Background job capacity is exhausted")
        self._background_job_count += 1
        started = False

        async def run():
            nonlocal started
            async with self.unit_of_work():
                started = True
                return await coroutine

        def finished(task):
            if not started:
                coroutine.close()
            self._jobs.discard(task)
            self._background_job_count -= 1

        token = self._current.set(None)
        background_token = self._background_scope.set(True)
        runner = run()
        try:
            task = tracked_task(runner, **task_options)
        except BaseException:
            runner.close()
            coroutine.close()
            self._background_job_count -= 1
            raise
        finally:
            self._background_scope.reset(background_token)
            self._current.reset(token)
        self._jobs.add(task)
        task.add_done_callback(finished)
        return task

    async def _shutdown(self) -> None:
        owners = tuple(self._owners.items())
        jobs = tuple(self._jobs)
        for task in set(self._owners) | set(jobs):
            task.cancel()
        # Wait for the unit, not the task: a task that has left its unit may be
        # awaiting this shutdown in its own close().
        await asyncio.gather(
            *(
                asyncio.wait({task, finished}, return_when=asyncio.FIRST_COMPLETED)
                for task, finished in owners
            ),
            *jobs,
            return_exceptions=True,
        )
        while self._workers:
            await asyncio.gather(*tuple(self._workers), return_exceptions=True)
        # A pending unit belongs to a task that finished without leaving it.
        if self._unclosed or any(not finished.done() for _task, finished in owners):
            raise DatabaseScopeError("Database shutdown has unconfirmed connection closures")

    async def close(self) -> None:
        lease = self._current.get()
        if asyncio.current_task() in self._owners or (lease is not None and lease.active):
            raise DatabaseScopeError("Close the manager outside a unit of work")
        previous = self._close_task
        # A failed shutdown described its moment, so a later close looks again.
        if previous is None or (
            previous.done() and (previous.cancelled() or previous.exception() is not None)
        ):
            self._closing = True
            self._close_task = asyncio.create_task(self._shutdown())
        # Shutdown keeps running if this caller is cancelled, and the caller can
        # still bound its own wait or be cancelled by a task it is waiting on.
        await asyncio.shield(self._close_task)

    def _cursor_operation(self, method: str, *args, **kwargs):
        lease = self._lease()

        async def run():
            conn = lease.require_active()
            cursor = await getattr(conn, method)(*args, **kwargs)
            return _Cursor(cursor, lease)

        return _CursorResult(run())

    def execute(self, sql, parameters=None):
        return self._cursor_operation("execute", sql, parameters)

    def executemany(self, sql, parameters):
        return self._cursor_operation("executemany", sql, parameters)

    def executescript(self, sql):
        return self._cursor_operation("executescript", sql)

    async def cursor(self):
        return await self._cursor_operation("cursor")

    async def execute_fetchall(self, sql, parameters=None):
        async with self.execute(sql, parameters) as cursor:
            return await cursor.fetchall()

    async def execute_insert(self, sql, parameters=None):
        # Keep insert + last_insert_rowid in one driver operation, as in the
        # existing wrapper, so joined tasks cannot interleave another insert.
        return await self._lease().require_active().execute_insert(sql, parameters)

    async def commit(self):
        await self._lease().require_active().commit()

    async def rollback(self):
        await self._lease().require_active().rollback()

    @property
    def in_transaction(self):
        return self._lease().require_active().in_transaction

    @property
    def row_factory(self):
        return self._lease().require_active().row_factory

    @row_factory.setter
    def row_factory(self, value):
        self._lease().require_active().row_factory = value

    @property
    def total_changes(self):
        return self._lease().require_active().total_changes


def spawn_database_task(db, coroutine, **task_options) -> asyncio.Task:
    """Detach work with database ownership; preserve legacy task scheduling."""
    if isinstance(db, RequestDatabase):
        return db.spawn(coroutine, **task_options)
    from genesis.util.tasks import tracked_task

    return tracked_task(coroutine, **task_options)
