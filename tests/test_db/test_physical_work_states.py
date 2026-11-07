"""Cancellation, executor faults and joined children over scratch resources."""

import asyncio
import contextvars
import inspect
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import anyio
import pytest

from genesis.db import request_database as scoped

pytestmark = pytest.mark.asyncio


@pytest.fixture
def physical_path(tmp_path):
    path = tmp_path / "states.db"
    sqlite3.connect(path).close()
    return path


class Gate:
    def __init__(self, *, error=None):
        self.entered = asyncio.Event()
        self.finished = asyncio.Event()
        self.release = threading.Event()
        self.loop = asyncio.get_running_loop()
        self.error = error

    def __call__(self):
        self.loop.call_soon_threadsafe(self.entered.set)
        try:
            assert self.release.wait(10), "fixture did not release physical work"
            if self.error:
                raise self.error
            return "finished"
        finally:
            self.loop.call_soon_threadsafe(self.finished.set)


async def test_requires_live_unit_before_submission(physical_path):
    db = scoped.RequestDatabase(physical_path, max_units=1)
    calls = []
    try:
        with pytest.raises(scoped.DatabaseScopeError):
            await db.run_in_thread(lambda: calls.append(True))
        async with db.unit_of_work():
            assert await db.run_in_thread(lambda value, *, suffix: value + suffix, "a", suffix="b") == "ab"
        with pytest.raises(scoped.DatabaseScopeError):
            await db.run_in_thread(lambda: calls.append(True))
        assert calls == [] and not db._closing
    finally:
        await db.close()


@pytest.mark.parametrize("kind", ["raw", "level"])
async def test_repeated_cancellation_and_context(physical_path, kind):
    db = scoped.RequestDatabase(physical_path, max_units=1)
    gate = Gate()
    caller = contextvars.ContextVar("synthetic_caller", default="absent")
    seen, scopes = [], []

    def operation():
        seen.append(caller.get())
        return gate()

    async def owner():
        token = caller.set("synthetic-owner")
        try:
            with anyio.CancelScope() as scope:
                scopes.append(scope)
                async with db.unit_of_work():
                    await db.run_in_thread(operation)
            assert kind == "level" and scope.cancelled_caught
        finally:
            caller.reset(token)

    task = asyncio.create_task(owner())
    try:
        await asyncio.wait_for(gate.entered.wait(), 10)
        if kind == "level":
            scopes[0].cancel()
        else:
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
        await asyncio.sleep(.03)
        assert not task.done() and db._slots._value == 0
        gate.release.set()
        await asyncio.gather(task, return_exceptions=True)
        assert seen == ["synthetic-owner"]
        assert task.cancelled() is (kind == "raw")
        assert db._slots._value == 1 and not db._owners and not db._workers
    finally:
        gate.release.set()
        await asyncio.gather(task, return_exceptions=True)
        await db.close()


async def test_physical_error_wins_after_caller_cancellation(physical_path):
    db = scoped.RequestDatabase(physical_path, max_units=1)
    gate = Gate(error=ValueError("synthetic physical failure"))

    async def owner():
        async with db.unit_of_work():
            await db.run_in_thread(gate)

    task = asyncio.create_task(owner())
    try:
        await asyncio.wait_for(gate.entered.wait(), 10)
        task.cancel()
        gate.release.set()
        with pytest.raises(ValueError, match="synthetic physical failure"):
            await task
        assert db._slots._value == 1
    finally:
        gate.release.set()
        await asyncio.gather(task, return_exceptions=True)
        await db.close()


async def test_joined_children_share_one_lease_but_all_delay_exit(physical_path):
    db = scoped.RequestDatabase(physical_path, max_units=1)
    gates = [Gate(), Gate()]
    children = []
    leaving = asyncio.Event()

    async def owner():
        async with db.unit_of_work():
            children.extend(asyncio.create_task(db.run_in_thread(gate)) for gate in gates)
            await asyncio.gather(*(gate.entered.wait() for gate in gates))
            leaving.set()
        return "unit finished"

    task = asyncio.create_task(owner())
    try:
        await asyncio.wait_for(leaving.wait(), 10)
        await asyncio.sleep(.02)
        assert len(db._owners) == 1 and db._slots._value == 0
        assert not task.done()
        gates[0].release.set()
        assert await asyncio.wait_for(children[0], 10) == "finished"
        assert not task.done() and db._slots._value == 0
        gates[1].release.set()
        assert await asyncio.wait_for(task, 10) == "unit finished"
        assert await children[1] == "finished"
        assert db._slots._value == 1
    finally:
        for gate in gates:
            gate.release.set()
        await asyncio.gather(task, *children, return_exceptions=True)
        await db.close()


async def test_cancelled_running_proxy_does_not_claim_physical_completion(physical_path, monkeypatch):
    from genesis.db.physical_work import PhysicalWork

    db = scoped.RequestDatabase(physical_path, max_units=1)
    gate = Gate()
    loop = asyncio.get_running_loop()
    real = loop.run_in_executor
    proxies = []

    def capture(executor, fn, *args):
        proxy = real(executor, fn, *args)
        if isinstance(getattr(fn, "__self__", None), PhysicalWork):
            proxies.append(proxy)
        return proxy

    monkeypatch.setattr(loop, "run_in_executor", capture)

    async def owner():
        async with db.unit_of_work():
            return await db.run_in_thread(gate)

    task = asyncio.create_task(owner())
    try:
        await asyncio.wait_for(gate.entered.wait(), 10)
        assert len(proxies) == 1
        proxies[0].cancel()
        await asyncio.sleep(.03)
        assert not task.done() and db._slots._value == 0
        gate.release.set()
        assert await asyncio.wait_for(task, 10) == "finished"
        assert db._slots._value == 1
    finally:
        gate.release.set()
        await asyncio.gather(task, return_exceptions=True)
        await db.close()


async def test_cancelled_queued_proxy_has_no_terminal_producer(physical_path, monkeypatch):
    from genesis.db.physical_work import PhysicalWork

    db = scoped.RequestDatabase(physical_path, max_units=1)
    loop = asyncio.get_running_loop()
    executor = ThreadPoolExecutor(max_workers=1)
    loop.set_default_executor(executor)
    blocker_release = threading.Event()
    blocker = executor.submit(blocker_release.wait, 10)
    real = loop.run_in_executor
    submitted, refused = asyncio.Event(), asyncio.Event()
    proxies, calls = [], []

    def capture(pool, fn, *args):
        proxy = real(pool, fn, *args)
        if isinstance(getattr(fn, "__self__", None), PhysicalWork):
            proxies.append(proxy)
            submitted.set()
        return proxy

    monkeypatch.setattr(loop, "run_in_executor", capture)

    async def owner():
        async with db.unit_of_work():
            try:
                await db.run_in_thread(lambda: calls.append(True))
            except scoped.DatabaseScopeError:
                refused.set()
                raise

    task = asyncio.create_task(owner())
    try:
        await asyncio.wait_for(submitted.wait(), 10)
        proxies[0].cancel()
        await asyncio.wait_for(refused.wait(), 10)
        assert db._closing and db._slots._value == 0
        blocker_release.set()
        with pytest.raises(scoped.DatabaseScopeError):
            await asyncio.wait_for(task, 10)
        with pytest.raises(scoped.DatabaseScopeError, match="unconfirmed"):
            await asyncio.wait_for(db.close(), 10)
        assert calls == [] and db._slots._value == 0
        assert db._unclosed and all(lease.closed for lease in db._unclosed)
    finally:
        blocker_release.set()
        await asyncio.gather(task, return_exceptions=True)
        blocker.result(timeout=10)
        executor.shutdown(wait=True)


async def test_submission_error_after_enqueue_keeps_ownership(physical_path, monkeypatch):
    from genesis.db.physical_work import PhysicalWork

    db = scoped.RequestDatabase(physical_path, max_units=1)
    gate = Gate()
    loop = asyncio.get_running_loop()
    real = loop.run_in_executor
    observed = asyncio.Event()

    def enqueue_then_fail(pool, fn, *args):
        proxy = real(pool, fn, *args)
        if isinstance(getattr(fn, "__self__", None), PhysicalWork):
            # The executor accepted the callable, but its caller gets no proxy.
            proxy.add_done_callback(lambda done: None if done.cancelled() else done.exception())
            raise RuntimeError("synthetic failure after enqueue")
        return proxy

    monkeypatch.setattr(loop, "run_in_executor", enqueue_then_fail)

    async def owner():
        async with db.unit_of_work():
            try:
                await db.run_in_thread(gate)
            except scoped.DatabaseScopeError:
                observed.set()
                raise

    task = asyncio.create_task(owner())
    try:
        await asyncio.wait_for(observed.wait(), 10)
        await asyncio.wait_for(gate.entered.wait(), 10)
        assert db._closing and db._slots._value == 0
        gate.release.set()
        await asyncio.wait_for(gate.finished.wait(), 10)
        with pytest.raises(scoped.DatabaseScopeError):
            await task
        await db.close()  # only an actual terminal can clear the held claim
        assert db._slots._value == 1
        with pytest.raises(scoped.DatabaseScopeError):
            async with db.unit_of_work():
                pytest.fail("admission reopened after failure")
    finally:
        gate.release.set()
        await asyncio.gather(task, return_exceptions=True)
        await db.close()


async def test_legacy_thread_helper_keeps_ordinary_cancellation():
    from genesis.db.request_database import run_resource_thread

    gate = Gate()
    task = asyncio.create_task(run_resource_thread(None, gate))
    try:
        await asyncio.wait_for(gate.entered.wait(), 10)
        task.cancel()
        await asyncio.sleep(.03)
        cancelled_before_release = task.done() and task.cancelled()
        finished_before_release = gate.finished.is_set()
        gate.release.set()
        await asyncio.gather(task, return_exceptions=True)
        assert cancelled_before_release and not finished_before_release
    finally:
        gate.release.set()
        await asyncio.wait_for(gate.finished.wait(), 10)


async def test_background_thread_retains_only_its_selected_pool(physical_path):
    db = scoped.RequestDatabase(physical_path, max_units=1,
                               max_background_units=1, max_background_jobs=2)
    gate = Gate()
    task = db.spawn(db.run_in_thread(gate))
    try:
        await asyncio.wait_for(gate.entered.wait(), 10)
        task.cancel()
        await asyncio.sleep(.03)
        assert not task.done() and db._background_slots._value == 0
        assert db._slots._value == 1 and db._background_job_count == 1
        async with db.unit_of_work():
            assert await db.execute_fetchall("SELECT 1")
        gate.release.set()
        await asyncio.gather(task, return_exceptions=True)
        assert task.cancelled() and db._background_slots._value == 1
        assert db._background_job_count == 0
    finally:
        gate.release.set()
        await asyncio.gather(task, return_exceptions=True)
        await db.close()


async def test_awaiter_creation_failure_closes_body_and_joins_physical_work(physical_path, monkeypatch):
    from genesis.db.physical_work import PhysicalWork

    db = scoped.RequestDatabase(physical_path, max_units=1)
    gate = Gate()
    real = asyncio.create_task
    unstarted = []

    def reject_result(coroutine, **options):
        if coroutine.cr_code is PhysicalWork.result.__code__:
            unstarted.append(coroutine)
            raise RuntimeError("synthetic observer creation failure")
        return real(coroutine, **options)

    monkeypatch.setattr(asyncio, "create_task", reject_result)

    async def owner():
        async with db.unit_of_work():
            await db.run_in_thread(gate)

    task = real(owner())
    try:
        await asyncio.wait_for(gate.entered.wait(), 10)
        assert not task.done() and db._slots._value == 0
        gate.release.set()
        with pytest.raises(RuntimeError, match="observer creation failure"):
            await task
        assert len(unstarted) == 1
        assert inspect.getcoroutinestate(unstarted[0]) == inspect.CORO_CLOSED
        assert db._slots._value == 1 and not db._workers
    finally:
        gate.release.set()
        await asyncio.gather(task, return_exceptions=True)
        for coroutine in unstarted:
            coroutine.close()
        await db.close()


@pytest.mark.parametrize("uncertain_first", [False, True])
async def test_uncertain_record_cannot_skip_known_running_sibling(physical_path, tmp_path, monkeypatch, uncertain_first):
    from genesis.db.physical_work import PhysicalWork

    db = scoped.RequestDatabase(physical_path, max_units=1)
    loop = asyncio.get_running_loop()
    entered, submitted, settling_known = asyncio.Event(), asyncio.Event(), asyncio.Event()
    release = threading.Event()
    resource = (tmp_path / "mixed-resource").open("w")
    writes, children = [], []
    real_submit, real_wait = loop.run_in_executor, PhysicalWork.wait

    def known():
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(10)
        try:
            resource.write("physical sibling")
            writes.append("used open handle")
        except ValueError:
            writes.append("actual closed-handle use")
            raise

    def uncertain():
        raise AssertionError("controlled submission never enqueues this body")

    class OrderedWork(set):
        # A set promises no iteration order. Exercise both permitted orders
        # without relying on object addresses or wall-clock timing.
        def __iter__(self):
            return iter(sorted(super().__iter__(), key=lambda work:
                               (work._fn is uncertain) != uncertain_first))

    def submit(pool, fn, *args):
        if getattr(getattr(fn, "__self__", None), "_fn", None) is uncertain:
            raise RuntimeError("controlled missing submission receipt")
        return real_submit(pool, fn, *args)

    async def observe_wait(work):
        current = asyncio.current_task().get_coro()
        if work._fn is known and current.cr_code is scoped.RequestDatabase._finish_unit.__code__:
            settling_known.set()
        return await real_wait(work)

    monkeypatch.setattr(loop, "run_in_executor", submit)
    monkeypatch.setattr(PhysicalWork, "wait", observe_wait)

    async def owner():
        try:
            async with db.unit_of_work():
                db._current.get().physical = OrderedWork()
                children.append(asyncio.create_task(db.run_in_thread(known)))
                await entered.wait()
                with pytest.raises(scoped.DatabaseScopeError):
                    await db.run_in_thread(uncertain)
                submitted.set()
        finally:
            resource.close()

    task = asyncio.create_task(owner())
    observation = asyncio.create_task(settling_known.wait())
    try:
        await asyncio.wait_for(submitted.wait(), 10)
        done, _ = await asyncio.wait({task, observation}, timeout=10,
                                     return_when=asyncio.FIRST_COMPLETED)
        assert done, "neither finalization nor known-work settlement was observed"
        retained = not task.done()
        release.set()
        await asyncio.gather(task, *children, return_exceptions=True)
        assert (retained, writes) == (True, ["used open handle"])
        with pytest.raises(scoped.DatabaseScopeError, match="unconfirmed"):
            await db.close()
        assert db._slots._value == 0 and not db._workers
        assert db._unclosed and all(lease.closed for lease in db._unclosed)
    finally:
        release.set()
        await asyncio.gather(task, *children, return_exceptions=True)
        observation.cancel()
        await asyncio.gather(observation, return_exceptions=True)
        resource.close()
