"""Tag maintenance ownership, admission and accepted-task single flight."""

import asyncio
import inspect
import sqlite3
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from genesis.db.request_database import RequestDatabase
from genesis.memory import intent
from genesis.memory.retrieval import HybridRetriever

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def state(tmp_path):
    intent._reset_tag_index_state()
    path = tmp_path / "tags.db"
    sqlite3.connect(path).close()
    db = RequestDatabase(path, max_units=1, max_background_units=1, max_background_jobs=1)
    client = MagicMock()
    client.get_collection.return_value = SimpleNamespace(points_count=1)
    client.scroll.return_value = ([SimpleNamespace(payload={"tags": ["alpha", "beta"]})], None)
    yield db, client
    await db.close()
    intent._reset_tag_index_state()


def capture_bodies(monkeypatch):
    original = intent._rebuild_tag_index
    bodies = []

    def build(*args, **kwargs):
        body = original(*args, **kwargs)
        bodies.append(body)
        return body

    monkeypatch.setattr(intent, "_rebuild_tag_index", build)
    return bodies


async def test_tag_scan_is_background_owned_and_drain_retains_real_file(state, tmp_path):
    db, client = state
    release = threading.Event()
    started, finished = asyncio.Event(), asyncio.Event()
    loop = asyncio.get_running_loop()
    resource = (tmp_path / "tag-provider").open("w")
    claims, errors = [], []

    def scroll(**kwargs):
        claims.append(db._lease())
        loop.call_soon_threadsafe(started.set)
        try:
            assert release.wait(10)
            resource.write("actual scan\n")
            resource.flush()
            return [SimpleNamespace(payload={"tags": ["alpha", "beta"]})], None
        except BaseException as exc:
            errors.append(type(exc).__name__)
            raise
        finally:
            loop.call_soon_threadsafe(finished.set)

    client.scroll.side_effect = scroll
    draining = None
    try:
        async with db.unit_of_work():
            foreground = db._lease()
            assert await intent.expand_query("alpha", client, ["episodic_memory"], db=db) == "alpha"
            assert len(db._jobs) == 1
        await asyncio.wait_for(started.wait(), 10)
        assert claims[0] is not foreground and claims[0].active
        assert db._slots._value == 1 and db._background_slots._value == 0
        async with db.unit_of_work():
            assert await db.execute_fetchall("SELECT 1")
        draining = asyncio.create_task(db.drain(grace_s=0))
        await asyncio.sleep(.05)
        assert not draining.done() and intent._rebuild_in_flight and not resource.closed
        release.set()
        await asyncio.wait_for(finished.wait(), 10)
        await asyncio.wait_for(draining, 10)
        resource.close()
        assert errors == []
        assert (tmp_path / "tag-provider").read_text() == "actual scan\n"
        assert not intent._rebuild_in_flight and not db._jobs and not db._owners
        assert db._background_slots._value == 1 and db._background_job_count == 0
    finally:
        release.set()
        if draining is not None:
            await asyncio.gather(draining, return_exceptions=True)
        await db.close()
        resource.close()


async def test_prestart_cancellation_closes_body_and_clears_flag(state, monkeypatch):
    db, client = state
    bodies = capture_bodies(monkeypatch)
    await intent.expand_query("alpha", client, ["episodic_memory"], db=db)
    assert intent._rebuild_in_flight and len(db._jobs) == 1
    task = next(iter(db._jobs))
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert inspect.getcoroutinestate(bodies[0]) == inspect.CORO_CLOSED
    assert not intent._rebuild_in_flight and not db._jobs
    client.scroll.assert_not_called()


@pytest.mark.parametrize("refusal", ["closing", "capacity", "creation-error"])
@pytest.mark.parametrize("warm", [False, True])
async def test_refusal_returns_stale_query_closes_body_and_resets(state, monkeypatch, refusal, warm):
    db, client = state
    bodies = capture_bodies(monkeypatch)
    if warm:
        intent._tag_index.build([["alpha", "beta"]], 1)
        client.get_collection.return_value = SimpleNamespace(points_count=2)
    if refusal == "closing":
        db.begin_draining()
    elif refusal == "capacity":
        db.spawn(asyncio.Event().wait())
    else:
        def fail(*args, **kwargs):
            raise RuntimeError("synthetic task creation failure")

        monkeypatch.setattr("genesis.util.tasks.tracked_task", fail)
    assert await intent.expand_query("alpha", client, ["episodic_memory"], db=db) == (
        "(alpha) OR beta" if warm else "alpha")
    assert len(bodies) == 1 and inspect.getcoroutinestate(bodies[0]) == inspect.CORO_CLOSED
    assert not intent._rebuild_in_flight
    client.scroll.assert_not_called()


async def test_scheduling_cancellation_propagates_after_body_and_flag_cleanup(state, monkeypatch):
    db, client = state
    bodies = capture_bodies(monkeypatch)

    def cancel(*args, **kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr("genesis.util.tasks.tracked_task", cancel)
    with pytest.raises(asyncio.CancelledError):
        await intent.expand_query("alpha", client, ["episodic_memory"], db=db)
    assert len(bodies) == 1 and inspect.getcoroutinestate(bodies[0]) == inspect.CORO_CLOSED
    assert not intent._rebuild_in_flight
    assert not db._jobs and db._background_job_count == 0
    client.scroll.assert_not_called()


@pytest.mark.parametrize("cancelled", [False, True])
async def test_legacy_creation_failure_closes_unscheduled_body(state, monkeypatch, cancelled):
    _db, client = state
    bodies = capture_bodies(monkeypatch)

    def fail(*args, **kwargs):
        raise asyncio.CancelledError if cancelled else RuntimeError("synthetic creation failure")

    monkeypatch.setattr("genesis.util.tasks.tracked_task", fail)
    if cancelled:
        with pytest.raises(asyncio.CancelledError):
            await intent.expand_query("alpha", client, ["episodic_memory"])
    else:
        assert await intent.expand_query("alpha", client, ["episodic_memory"]) == "alpha"
    assert len(bodies) == 1 and inspect.getcoroutinestate(bodies[0]) == inspect.CORO_CLOSED
    assert not intent._rebuild_in_flight
    client.scroll.assert_not_called()


async def test_background_caller_under_pressure_never_waits_for_its_child(state):
    db, client = state

    async def parent():
        assert db._background_slots._value == 0
        result = await intent.expand_query("alpha", client, ["episodic_memory"], db=db)
        assert not intent._rebuild_in_flight
        return result

    task = db.spawn(parent())
    assert await asyncio.wait_for(task, 5) == "alpha"
    assert not intent._rebuild_in_flight
    client.scroll.assert_not_called()


@pytest.mark.parametrize("failure", [False, True])
async def test_only_accepted_task_completion_clears_flag(state, monkeypatch, failure):
    """A completed body cannot admit a new job before its done callback."""
    db, client = state
    callbacks, bodies = [], []
    real_create = asyncio.create_task

    class DelayedDone:
        def add_done_callback(self, callback):
            callbacks.append(callback)

    def schedule(_db, coroutine, **options):
        bodies.append(real_create(coroutine))
        return DelayedDone()

    monkeypatch.setattr("genesis.db.request_database.spawn_database_task", schedule)
    monkeypatch.setattr(intent._tag_index, "is_stale", lambda _count: True)
    if failure:
        def fail(*args):
            raise RuntimeError("synthetic index build failure")

        monkeypatch.setattr(intent._tag_index, "build", fail)
    # This fixture deliberately delays the accepted-task callback, including
    # all wrapper cleanup, after the body has already completed.
    await intent.expand_query("alpha", client, ["episodic_memory"], db=None)
    outcomes = await asyncio.gather(bodies[0], return_exceptions=True)
    assert isinstance(outcomes[0], RuntimeError) == failure
    assert intent._rebuild_in_flight
    await intent.expand_query("alpha", client, ["episodic_memory"], db=None)
    assert len(bodies) == 1
    # The body completed but is not allowed to release single flight itself.
    callbacks[0](bodies[0])
    assert not intent._rebuild_in_flight


async def test_retriever_forwards_its_facade_to_expansion(state, monkeypatch):
    db, client = state
    received = []

    async def expand(query, qdrant, collections, **options):
        received.append(options.get("db"))
        return query

    monkeypatch.setattr("genesis.memory.retrieval.expand_query", expand)
    retriever = HybridRetriever(embedding_provider=SimpleNamespace(), qdrant_client=client, db=db)
    assert await retriever._expand_fts_query("alpha", ["episodic_memory"], expand_query_terms=True) == ("alpha", None)
    assert received == [db]
