"""Explicit, bounded background admission over real scratch SQLite."""

import asyncio
import inspect
import sqlite3

import pytest

from genesis.db import request_database as scoped

pytestmark = pytest.mark.asyncio


@pytest.fixture
def database_path(tmp_path):
    path = tmp_path/'admission.db'
    with sqlite3.connect(path) as connection:
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('CREATE TABLE samples (value TEXT)')
    return path


@pytest.fixture
async def make_manager(database_path):
    managers = []

    def make(**overrides):
        options = dict(max_units=1, checkout_timeout_s=.05,
                       max_background_units=1, max_background_jobs=4)
        options.update(overrides)
        manager = scoped.RequestDatabase(database_path, **options)
        managers.append(manager)
        return manager

    yield make
    for manager in managers:
        await manager.close()


async def test_foreground_only_manager_does_not_allocate_background_capacity(database_path):
    manager = scoped.RequestDatabase(database_path, max_units=1)

    async def body():
        raise AssertionError('unconfigured background work must not start')

    coroutine = body()
    try:
        with pytest.raises(ValueError, match='background'):
            manager.spawn(coroutine)
        assert inspect.getcoroutinestate(coroutine) == inspect.CORO_CLOSED
        async with manager.unit_of_work():
            assert await manager.execute_fetchall('SELECT value FROM samples') == []
    finally:
        await manager.close()


@pytest.mark.parametrize('field,value', [
    ('max_background_units', True), ('max_background_units', False),
    ('max_background_units', 0), ('max_background_units', -1),
    ('max_background_units', 1.5), ('max_background_units', '1'),
    ('max_background_jobs', True), ('max_background_jobs', False),
    ('max_background_jobs', 0), ('max_background_jobs', -1),
    ('max_background_jobs', 1.5), ('max_background_jobs', '1'),
    ('max_background_units', None), ('max_background_jobs', None),
])
async def test_background_budgets_are_explicit_positive_counts(database_path, field, value):
    options = dict(max_units=1, max_background_units=1, max_background_jobs=2)
    options[field] = value
    with pytest.raises(ValueError, match='background'):
        scoped.RequestDatabase(database_path, **options)


async def test_accepted_jobs_wait_and_overflow_is_synchronous(make_manager):
    manager = make_manager(max_background_jobs=2)
    started = asyncio.Event()
    release = asyncio.Event()
    background_connection = []

    async def writer(value):
        background_connection.append(manager._lease().require_active())
        await manager.execute('INSERT INTO samples VALUES (?)', (value,))
        await manager.commit()
        started.set()
        await release.wait()

    first = manager.spawn(writer('first'))
    second = manager.spawn(writer('second'))
    try:
        await asyncio.wait_for(started.wait(), 10)
        await asyncio.sleep(.1)  # exceeds foreground checkout_timeout_s=.05
        assert not second.done()
        refused = writer('refused')
        with pytest.raises(scoped.BackgroundTaskCapacityError):
            manager.spawn(refused)
        assert inspect.getcoroutinestate(refused) == inspect.CORO_CLOSED
        async with manager.unit_of_work():
            assert manager._lease().require_active() is not background_connection[0]
            assert len(await manager.execute_fetchall('SELECT value FROM samples')) == 1
    finally:
        release.set()
        outcomes = await asyncio.gather(first, second, return_exceptions=True)
    assert outcomes == [None, None]
    async with manager.unit_of_work():
        assert len(await manager.execute_fetchall('SELECT value FROM samples')) == 2


async def test_prestart_cancel_returns_the_job_reservation(make_manager):
    manager = make_manager(max_background_jobs=1)

    async def body():
        await manager.execute("INSERT INTO samples VALUES ('written')")

    coroutine = body()
    task = manager.spawn(coroutine)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert inspect.getcoroutinestate(coroutine) == inspect.CORO_CLOSED
    await manager.spawn(body())
    async with manager.unit_of_work():
        assert len(await manager.execute_fetchall('SELECT value FROM samples')) == 1


async def test_creation_failure_returns_reservation_and_parent_context(make_manager, monkeypatch):
    from genesis.util import tasks

    manager = make_manager(max_background_jobs=1)

    async def body():
        await manager.execute("INSERT INTO samples VALUES ('accepted')")

    def fail(*args, **kwargs):
        raise RuntimeError('synthetic creation failure')

    rejected = body()
    with monkeypatch.context() as patch:
        patch.setattr(tasks, 'tracked_task', fail)
        async with manager.unit_of_work():
            with pytest.raises(RuntimeError, match='synthetic creation failure'):
                manager.spawn(rejected)
            assert await manager.execute_fetchall('SELECT value FROM samples') == []
    assert inspect.getcoroutinestate(rejected) == inspect.CORO_CLOSED
    await manager.spawn(body())
    assert not manager._jobs


async def test_background_capacity_waits_for_actual_cleanup(make_manager, monkeypatch):
    manager = make_manager(max_background_jobs=2)
    finishing = asyncio.Event()
    finish_release = asyncio.Event()
    second_started = asyncio.Event()
    leases = []
    original = manager._finish

    async def delayed_finish(opening, lease, success):
        if leases and lease is leases[0]:
            finishing.set()
            await finish_release.wait()
        await original(opening, lease, success)

    monkeypatch.setattr(manager, '_finish', delayed_finish)

    async def first_body():
        leases.append(manager._current.get())
        await manager.execute("INSERT INTO samples VALUES ('first')")
        await manager.commit()

    async def second_body():
        second_started.set()
        await manager.execute("INSERT INTO samples VALUES ('second')")

    first = manager.spawn(first_body())
    await asyncio.wait_for(finishing.wait(), 10)
    second = manager.spawn(second_body())
    try:
        await asyncio.sleep(.1)
        assert not second_started.is_set()
        assert leases[0].connection._connection is not None
        async with manager.unit_of_work():
            assert len(await manager.execute_fetchall('SELECT value FROM samples')) == 1
    finally:
        finish_release.set()
        await asyncio.gather(first, second)
    assert leases[0].closed and second_started.is_set()


async def test_shutdown_joins_both_pools_and_background_waiters(make_manager):
    manager = make_manager(max_background_jobs=2)
    entered = [asyncio.Event() for _ in range(2)]

    async def foreground():
        async with manager.unit_of_work():
            entered[0].set()
            await asyncio.Event().wait()

    async def background():
        entered[1].set()
        await asyncio.Event().wait()

    foreground_task = asyncio.create_task(foreground())
    bodies = [background(), background()]
    jobs = [manager.spawn(body) for body in bodies]
    await asyncio.wait_for(asyncio.gather(*(event.wait() for event in entered)), 10)
    await manager.close()
    await asyncio.gather(foreground_task, *jobs, return_exceptions=True)
    assert foreground_task.cancelled() and all(task.cancelled() for task in jobs)
    assert all(inspect.getcoroutinestate(body) == inspect.CORO_CLOSED for body in bodies)
    assert not manager._owners and not manager._workers and not manager._jobs


async def test_closing_refusal_precedes_capacity_refusal(make_manager):
    manager = make_manager(max_background_jobs=1)
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    finish_release = asyncio.Event()

    async def body():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
            await finish_release.wait()

    running = manager.spawn(body())
    await asyncio.wait_for(entered.wait(), 10)
    closing = asyncio.create_task(manager.close())
    await asyncio.wait_for(cancelled.wait(), 10)
    rejected = body()
    try:
        with pytest.raises(scoped.DatabaseScopeError, match='closing'):
            manager.spawn(rejected)
        assert inspect.getcoroutinestate(rejected) == inspect.CORO_CLOSED
    finally:
        finish_release.set()
        await closing
        await asyncio.gather(running, return_exceptions=True)
