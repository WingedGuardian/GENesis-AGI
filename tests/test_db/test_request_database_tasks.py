"""Detached database work owns a fresh unit and is drained at shutdown."""

import asyncio
import inspect
from contextvars import ContextVar
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from genesis.db import request_database as scoped
from genesis.db.connection import get_db
from genesis.db.schema import create_all_tables

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def database(tmp_path):
    path = tmp_path / "tasks.db"
    setup = await get_db(path)
    await create_all_tables(setup)
    await setup.execute("CREATE TABLE samples (value TEXT)")
    await setup.commit()
    await setup.close()
    manager = scoped.RequestDatabase(path, max_units=2, max_background_units=2, max_background_jobs=4)
    try:
        yield manager
    finally:
        await manager.close()


async def test_detached_work_survives_parent_rollback_preserving_other_context(database):
    caller = ContextVar("synthetic_caller", default="missing")
    caller.set("caller-a")
    release = asyncio.Event()
    seen = []

    async def writer():
        seen.append(caller.get())
        await release.wait()
        await database.execute("INSERT INTO samples VALUES ('detached')")

    with pytest.raises(ValueError):
        async with database.unit_of_work():
            task = scoped.spawn_database_task(database, writer(), name="detached-write")
            await database.execute("INSERT INTO samples VALUES ('rolled-back')")
            raise ValueError("request failed")
    caller.set("caller-b")
    release.set()
    await task
    async with database.unit_of_work():
        rows = await database.execute_fetchall("SELECT value FROM samples")
    assert [row[0] for row in rows] == ["detached"]
    assert seen == ["caller-a"]


async def test_detached_failure_rolls_back_and_is_observable(database):
    async def fail():
        await database.execute("INSERT INTO samples VALUES ('partial')")
        raise ValueError("background failure")

    task = scoped.spawn_database_task(database, fail(), name="failed-write")
    with pytest.raises(ValueError, match="background failure"):
        await task
    async with database.unit_of_work():
        assert await database.execute_fetchall("SELECT * FROM samples") == []


@pytest.mark.parametrize("before_start", [False, True])
async def test_cancellation_closes_body_and_releases_tracking(database, before_start):
    entered = asyncio.Event()

    async def body():
        entered.set()
        await asyncio.Event().wait()

    coro = body()
    task = scoped.spawn_database_task(database, coro)
    if not before_start:
        await entered.wait()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert inspect.getcoroutinestate(coro) == inspect.CORO_CLOSED
    assert not database._jobs
    assert not database._owners


async def test_shutdown_accounts_for_prestart_and_waiting_jobs(database):
    jobs = []
    started = asyncio.Event()

    async def body():
        started.set()
        await asyncio.Event().wait()

    # Three jobs compete for two slots; shutdown must include the waiter too.
    for _ in range(3):
        jobs.append(scoped.spawn_database_task(database, body()))
    await started.wait()
    await database.close()
    assert all(job.cancelled() for job in jobs)
    assert not database._jobs and not database._owners and not database._workers
    coro = body()
    with pytest.raises(scoped.DatabaseScopeError, match="closing"):
        scoped.spawn_database_task(database, coro)
    assert inspect.getcoroutinestate(coro) == inspect.CORO_CLOSED


async def test_activity_and_embedding_heartbeat_use_independent_units(database):
    from genesis.memory.retrieval import HybridRetriever
    from genesis.observability.provider_activity import ProviderActivityTracker

    tracker = ProviderActivityTracker()
    tracker.set_db(database)
    retriever = HybridRetriever(
        embedding_provider=SimpleNamespace(embed=AsyncMock(return_value=[.1, .2])),
        qdrant_client=None, db=database,
    )
    with pytest.raises(ValueError):
        async with database.unit_of_work():
            await database.execute("INSERT INTO samples VALUES ('partial')")
            tracker.record("synthetic", latency_ms=1, success=True)
            assert await retriever._embed_query("synthetic") == ([.1, .2], True)
            raise ValueError("parent fails")
    await tracker._flush_task
    # This is a scratch test: wait for the manager's owned jobs, not shutdown
    # (shutdown deliberately cancels outstanding best-effort work).
    await asyncio.gather(*tuple(database._jobs))
    async with database.unit_of_work():
        assert await database.execute_fetchall("SELECT * FROM samples") == []
        assert len(await database.execute_fetchall("SELECT * FROM activity_log")) == 1
        assert len(await database.execute_fetchall("SELECT * FROM call_site_last_run")) == 1


async def test_deferred_reservation_released_on_prestart_cancel(database):
    from genesis.memory import retrieval

    async def body():
        await database.execute("INSERT INTO samples VALUES ('not-started')")

    baseline = retrieval._deferred_side_effects_inflight()
    coro = body()
    retrieval._schedule_deferred_side_effects(coro, name="prestart", db=database)
    assert retrieval._deferred_side_effects_inflight() == baseline + 1
    jobs = tuple(database._jobs)
    for job in jobs:
        job.cancel()
    await asyncio.gather(*jobs, return_exceptions=True)
    assert retrieval._deferred_side_effects_inflight() == baseline
    assert inspect.getcoroutinestate(coro) == inspect.CORO_CLOSED


async def test_procedure_refresh_cancel_before_body_releases_guard(database, monkeypatch):
    from genesis.memory import proactive

    cache = proactive._ProcedureCache(matrix=None, meta=[], built_at=0)
    monkeypatch.setattr(proactive, "_procedure_cache", cache)
    monkeypatch.setattr(proactive, "_procedure_refresh_in_flight", False)
    assert await proactive._load_procedure_cache(database) is cache
    assert proactive._procedure_refresh_in_flight
    jobs = tuple(database._jobs)
    assert len(jobs) == 1
    jobs[0].cancel()
    await asyncio.gather(*jobs, return_exceptions=True)
    assert not proactive._procedure_refresh_in_flight


async def test_rejected_deferred_job_releases_reservation(database):
    from genesis.memory import retrieval

    await database.close()
    baseline = retrieval._deferred_side_effects_inflight()

    async def body():
        raise AssertionError("must never run")

    coro = body()
    assert retrieval._schedule_deferred_side_effects(coro, name="rejected", db=database) is None
    assert retrieval._deferred_side_effects_inflight() == baseline
    assert inspect.getcoroutinestate(coro) == inspect.CORO_CLOSED


async def test_legacy_helper_keeps_caller_connection(db):
    async def body():
        await db.execute("CREATE TABLE legacy_task (id INTEGER)")
        return 42

    assert await scoped.spawn_database_task(db, body(), name="legacy") == 42
    assert await db.execute_fetchall("SELECT * FROM legacy_task") == []


async def test_background_burst_does_not_starve_request_or_lose_writes(database):
    manager = scoped.RequestDatabase(database.path, max_units=2, checkout_timeout_s=.5,
                                     max_background_units=2, max_background_jobs=4)
    entered = [asyncio.Event() for _ in range(2)]
    release = asyncio.Event()

    async def writer(index):
        await manager.execute("INSERT INTO samples VALUES (?)", (str(index),))
        await manager.commit()
        if index < 2:
            entered[index].set()
        # Deliberately hold the job outside DB work, as the real producers do.
        await release.wait()

    jobs = [manager.spawn(writer(index), name=f"burst-{index}") for index in range(4)]
    foreground_error = None
    try:
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in entered)), 10)
        try:
            async with manager.unit_of_work():
                rows = await manager.execute_fetchall("SELECT value FROM samples")
                assert len(rows) == 2
        except TimeoutError as error:
            foreground_error = error
    finally:
        release.set()
        outcomes = await asyncio.gather(*jobs, return_exceptions=True)
        await manager.close()
    assert foreground_error is None, "background non-DB work starved foreground admission"
    assert outcomes == [None] * 4, outcomes
    async with database.unit_of_work():
        assert len(await database.execute_fetchall("SELECT value FROM samples")) == 4
    assert not manager._jobs and not manager._owners and not manager._workers


async def test_deferred_capacity_refusal_releases_reservation_and_allows_inline(database):
    from genesis.memory import retrieval

    release = asyncio.Event()

    async def hold():
        await release.wait()

    blockers = [database.spawn(hold()) for _ in range(4)]

    async def writer():
        await database.execute("INSERT INTO samples VALUES ('inline-fallback')")

    coroutine = writer()
    baseline = retrieval._deferred_side_effects_inflight()
    try:
        assert retrieval._schedule_deferred_side_effects(coroutine, name='full', db=database) is False
        assert inspect.getcoroutinestate(coroutine) == inspect.CORO_CLOSED
        assert retrieval._deferred_side_effects_inflight() == baseline
        async with database.unit_of_work():
            await writer()  # fresh instance; the refused body never ran
    finally:
        release.set()
        await asyncio.gather(*blockers)
    async with database.unit_of_work():
        assert [row[0] for row in await database.execute_fetchall('SELECT value FROM samples')] == ['inline-fallback']


async def test_heartbeat_refusal_does_not_fail_a_successful_embedding(database):
    from genesis.memory.retrieval import HybridRetriever

    release = asyncio.Event()

    async def hold():
        await release.wait()

    blockers = [database.spawn(hold()) for _ in range(4)]
    retriever = HybridRetriever(
        embedding_provider=SimpleNamespace(embed=AsyncMock(return_value=[.1, .2])),
        qdrant_client=None, db=database,
    )
    try:
        assert await retriever._embed_query('synthetic') == ([.1, .2], True)
    finally:
        release.set()
        await asyncio.gather(*blockers)


async def test_activity_batch_survives_capacity_refusal_and_retries(database, caplog):
    import logging

    from genesis.observability.provider_activity import ProviderActivityTracker

    release = asyncio.Event()

    async def hold():
        await release.wait()

    blockers = [database.spawn(hold()) for _ in range(4)]
    tracker = ProviderActivityTracker()
    tracker.set_db(database)
    caplog.set_level(logging.DEBUG, logger='genesis.observability.provider_activity')
    try:
        tracker.record('synthetic', latency_ms=1, success=True)
        assert len(tracker._write_batch) == 1
        assert 'event loop closed' not in caplog.text
    finally:
        release.set()
        await asyncio.gather(*blockers)
    tracker.record('synthetic', latency_ms=2, success=True)
    await tracker._flush_task
    async with database.unit_of_work():
        assert len(await database.execute_fetchall('SELECT * FROM activity_log')) == 2


async def test_bookmark_capacity_refusal_retains_pending_marker(database, tmp_path, monkeypatch):
    from genesis.mcp import memory as memory_mod

    release = asyncio.Event()

    async def hold():
        await release.wait()

    blockers = [database.spawn(hold()) for _ in range(4)]
    marker = tmp_path/'pending.json'
    marker.write_text('{}')
    bodies = []

    def pending(*args):
        async def process():
            marker.unlink()
        coroutine = process()
        bodies.append(coroutine)
        return coroutine

    monkeypatch.setattr(memory_mod, '_store', object())
    monkeypatch.setattr(memory_mod, '_retriever', object())
    monkeypatch.setattr(memory_mod, '_db', database)
    monkeypatch.setattr(memory_mod, '_process_pending_bookmarks', True)
    monkeypatch.setattr(memory_mod, '_PLAN_BOOKMARK_PENDING', marker)
    monkeypatch.setattr(memory_mod, '_process_plan_bookmark_pending', pending)
    try:
        memory_mod._require_init()
        assert marker.read_text() == '{}'
        assert len(bodies) == 1 and inspect.getcoroutinestate(bodies[0]) == inspect.CORO_CLOSED
    finally:
        release.set()
        await asyncio.gather(*blockers)
