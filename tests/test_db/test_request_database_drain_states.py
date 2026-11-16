"""Admission and shared shutdown policy over real scratch connections."""

import asyncio
import inspect
import sqlite3

import pytest

from genesis.db.request_database import DatabaseScopeError, RequestDatabase

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def manager(tmp_path):
    path = tmp_path / "drain-states.db"
    sqlite3.connect(path).close()
    db = RequestDatabase(path, max_units=1, max_background_units=1, max_background_jobs=2)
    yield db
    await db.close()


@pytest.mark.parametrize("grace", [True, False, -1, float("inf"), float("nan"), "1", None, 1j])
async def test_invalid_grace_does_not_close_admission(manager, grace):
    with pytest.raises(ValueError, match="grace"):
        await manager.drain(grace_s=grace)
    assert not manager._closing and manager._close_task is None
    async with manager.unit_of_work():
        assert await manager.execute_fetchall("SELECT 1")


@pytest.mark.parametrize("method", ["begin_draining", "drain", "close"])
async def test_shutdown_from_joined_child_refuses_without_mutation(manager, method):
    async def child():
        with pytest.raises(DatabaseScopeError):
            result = getattr(manager, method)(**({"grace_s": 0} if method == "drain" else {}))
            if inspect.isawaitable(result):
                await result

    async with manager.unit_of_work():
        await asyncio.create_task(child())
        assert not manager._closing
        assert await manager.execute_fetchall("SELECT 1")


async def test_begin_draining_refuses_prestart_jobs_and_closes_body(manager):
    calls = []

    async def body():
        calls.append(True)

    coroutine = body()
    task = manager.spawn(coroutine)
    manager.begin_draining()
    manager.begin_draining()
    assert manager._close_task is None
    await manager.drain(grace_s=0)
    assert task.done() and coroutine.cr_frame is None and calls == []
    assert not manager._jobs and manager._background_job_count == 0
    with pytest.raises(DatabaseScopeError):
        async with manager.unit_of_work():
            pytest.fail("draining admitted new work")


async def test_expired_grace_cancels_then_joins_owner(manager):
    entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def owner():
        async with manager.unit_of_work():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
                await release.wait()

    task = asyncio.create_task(owner())
    await entered.wait()
    closing = asyncio.create_task(manager.drain(grace_s=.03))
    try:
        await asyncio.wait_for(cancelled.wait(), 10)
        assert not closing.done() and manager._slots._value == 0
        release.set()
        await asyncio.wait_for(closing, 10)
        assert task.cancelled() and manager._slots._value == 1
    finally:
        release.set()
        await asyncio.gather(task, closing, return_exceptions=True)


async def test_first_drain_policy_survives_later_close_and_cancelled_waiter(manager):
    entered, release = asyncio.Event(), asyncio.Event()

    async def owner():
        async with manager.unit_of_work():
            entered.set()
            await release.wait()

    task = asyncio.create_task(owner())
    await entered.wait()
    draining = asyncio.create_task(manager.drain(grace_s=1))
    try:
        while manager._close_task is None:
            await asyncio.sleep(0)
        shutdown = manager._close_task
        draining.cancel()
        await asyncio.gather(draining, return_exceptions=True)
        closing = asyncio.create_task(manager.close())
        await asyncio.sleep(.03)
        assert not shutdown.cancelled() and not closing.done() and not task.done()
        assert manager._close_task is shutdown
        release.set()
        await asyncio.wait_for(closing, 10)
        assert not task.cancelled()
    finally:
        release.set()
        await asyncio.gather(task, draining, return_exceptions=True)


async def test_registered_checkout_waiter_can_finish_during_grace(manager):
    entered, release = asyncio.Event(), asyncio.Event()
    completed = []

    async def owner(first):
        async with manager.unit_of_work():
            if first:
                entered.set()
                await release.wait()
            await manager.execute_fetchall("SELECT 1")
            completed.append(first)

    first = asyncio.create_task(owner(True))
    await entered.wait()
    second = asyncio.create_task(owner(False))
    closing = None
    try:
        async with asyncio.timeout(10):
            while len(manager._owners) != 2:
                await asyncio.sleep(0)
        closing = asyncio.create_task(manager.drain(grace_s=1))
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(first, second, closing)
        assert completed == [True, False]
    finally:
        release.set()
        await asyncio.gather(first, second, *([closing] if closing else []), return_exceptions=True)
