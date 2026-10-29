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
    manager = scoped.RequestDatabase(path, max_units=2)
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
    with pytest.raises(scoped.DatabaseScopeError):
        retrieval._schedule_deferred_side_effects(coro, name="rejected", db=database)
    assert retrieval._deferred_side_effects_inflight() == baseline
    assert inspect.getcoroutinestate(coro) == inspect.CORO_CLOSED


async def test_legacy_helper_keeps_caller_connection(db):
    async def body():
        await db.execute("CREATE TABLE legacy_task (id INTEGER)")
        return 42

    assert await scoped.spawn_database_task(db, body(), name="legacy") == 42
    assert await db.execute_fetchall("SELECT * FROM legacy_task") == []
