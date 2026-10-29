"""The opt-in service initializer uses real schemas and real init functions."""

import asyncio
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastmcp import Client, FastMCP
from fastmcp.server.middleware import Middleware

from genesis.db.connection import drain_connection_worker, get_db
from genesis.db.request_database import DatabaseScopeError, RequestDatabase
from genesis.db.schema import create_all_tables
from genesis.mcp.request_service import initialize_request_service
from genesis.mcp.standalone_health import StandaloneHealthDataService
from genesis.observability.mcp_middleware import InstrumentationMiddleware
from genesis.observability.provider_activity import ProviderActivityTracker

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def database(tmp_path):
    path = tmp_path / "init.db"
    setup = await get_db(path)
    await create_all_tables(setup)
    await setup.commit()
    await setup.close()
    db = RequestDatabase(path, max_units=4)
    try:
        yield db
    finally:
        await db.close()


def isolate_module(monkeypatch, name):
    module = import_module(f"genesis.mcp.{name}" if name in {"memory", "health"} else f"genesis.mcp.{name}_mcp")
    for key in (
        "_db", "_store", "_retriever", "_qdrant", "_bookmark_mgr", "_user_model_evolver",
        "_process_pending_bookmarks", "_service", "_event_bus", "_activity_tracker",
        "_job_retry_registry", "_router", "_pipeline", "_memory_store", "_surplus_queue",
        "_engagement", "_config",
    ):
        if hasattr(module, key):
            monkeypatch.setattr(module, key, getattr(module, key))
    monkeypatch.setattr(module, "mcp", FastMCP(f"scoped-{name}"))
    return module


def bootstrap_dependencies(name, db):
    if name == "memory":
        return dict(embedding_provider=object(), qdrant_client=object(), process_pending_bookmarks=False)
    if name == "health":
        return dict(service=StandaloneHealthDataService(status_path=db.path.parent / "status.json", db=db))
    if name == "outreach":
        return dict(pipeline=None, engagement=None, config=None)
    return {}


@pytest.mark.parametrize("name", ["memory", "health", "recon", "outreach"])
@pytest.mark.parametrize("failure", ["commit", "cleanup"])
async def test_failed_bootstrap_finalization_never_registers(database, monkeypatch, name, failure):
    from genesis.db import request_database

    module = isolate_module(monkeypatch, name)
    manager = RequestDatabase(database.path, max_units=1)
    key = "_service" if name == "health" else "_db"
    prior = getattr(module, key)
    middleware = list(module.mcp.middleware)
    commits = 0
    connections = []

    async def factory(*args, **kwargs):
        conn = await get_db(*args, **kwargs)
        raw = conn._conn
        connections.append(raw)
        real_commit, real_close = raw.commit, raw.close

        async def commit():
            nonlocal commits
            commits += 1
            # Outreach ensure_table commits explicitly before unit finalization.
            if failure == "commit" and commits == (2 if name == "outreach" else 1):
                raise RuntimeError("synthetic bootstrap finalization failure")
            await real_commit()

        async def close():
            await real_close()
            await drain_connection_worker(raw)
            if failure == "cleanup":
                raise RuntimeError("synthetic bootstrap finalization failure")

        monkeypatch.setattr(raw, "commit", commit)
        monkeypatch.setattr(raw, "close", close)
        return conn

    monkeypatch.setattr(request_database, "get_db", factory)
    try:
        with pytest.raises(RuntimeError, match="synthetic bootstrap finalization failure"):
            await initialize_request_service(name, db=manager, **bootstrap_dependencies(name, manager))
        assert getattr(module, key) is prior
        assert module.mcp.middleware == middleware
        assert commits == (2 if name == "outreach" else 1)
        assert not manager._owners and not manager._workers and not manager._jobs
        assert all(not getattr(conn, "_thread", conn).is_alive() for conn in connections)
    finally:
        if failure == "cleanup":
            # A close error must keep the manager quarantined even though this
            # instrument closed the physical connection before raising.
            with pytest.raises(DatabaseScopeError, match="closure"):
                await manager.close()
        else:
            await manager.close()


@pytest.mark.parametrize("name", ["memory", "health", "recon", "outreach"])
async def test_bootstrap_rejects_joining_callers_unit_before_registration(database, monkeypatch, name):
    module = isolate_module(monkeypatch, name)
    key = "_service" if name == "health" else "_db"
    prior = getattr(module, key)
    middleware = list(module.mcp.middleware)
    async with database.unit_of_work():
        with pytest.raises(DatabaseScopeError, match="Independent work"):
            await initialize_request_service(name, db=database, **bootstrap_dependencies(name, database))
        assert getattr(module, key) is prior
        assert module.mcp.middleware == middleware
        assert (await database.execute_fetchall("SELECT 1"))[0][0] == 1


@pytest.mark.parametrize("name", ["memory", "health", "recon", "outreach"])
async def test_every_service_installs_boundary_without_tracker_and_reinitializes(database, monkeypatch, name):
    module = isolate_module(monkeypatch, name)
    dependencies = {}
    if name == "memory":
        dependencies = dict(embedding_provider=object(), qdrant_client=object(), process_pending_bookmarks=False)
    elif name == "health":
        dependencies = dict(service=StandaloneHealthDataService(status_path=database.path.parent / "status.json", db=database))
    elif name == "outreach":
        dependencies = dict(pipeline=None, engagement=None, config=None)
        async with database.unit_of_work():
            await database.execute("DROP TABLE pending_outreach")
    for _ in range(2):
        server = await initialize_request_service(name, db=database, **dependencies)
        assert server is module.mcp
        assert len([m for m in server.middleware if isinstance(m, InstrumentationMiddleware)]) == 1

    @server.tool()
    async def schema_count() -> int:
        rows = await database.execute_fetchall("SELECT count(*) FROM sqlite_master WHERE type='table'")
        return rows[0][0]

    async with Client(server) as client:
        assert (await client.call_tool("schema_count")).data > 0
    if name == "outreach":
        async with database.unit_of_work():
            assert await database.execute_fetchall("SELECT * FROM pending_outreach") == []
    assert not database._owners and not database._workers


async def test_tracker_warmup_uses_bootstrap_unit(database, monkeypatch):
    isolate_module(monkeypatch, "recon")
    isolate_module(monkeypatch, "health")
    from datetime import UTC, datetime

    async with database.unit_of_work():
        await database.execute(
            "INSERT INTO activity_log(provider,latency_ms,success,cache_hit,created_at) VALUES (?,1,1,0,?)",
            ("synthetic", datetime.now(UTC).isoformat()),
        )
    tracker = ProviderActivityTracker()
    tracker.set_db(database)
    async with database.unit_of_work():
        await tracker.warm_from_db()
    tracker.record("synthetic-live", latency_ms=2, success=True)
    await tracker._flush_task
    await initialize_request_service("recon", db=database, activity_tracker=tracker)
    await initialize_request_service("recon", db=database, activity_tracker=tracker)
    service = StandaloneHealthDataService(status_path=database.path.parent / "status.json", db=database)
    await initialize_request_service("health", db=database, activity_tracker=tracker, service=service)
    assert tracker.summary("synthetic")["calls"] == 1
    assert tracker.summary("synthetic-live")["calls"] == 1
    assert not database._owners


async def test_scoped_memory_rejects_incompatible_read_pool(database, monkeypatch):
    module = isolate_module(monkeypatch, "memory")
    prior = module._db
    with pytest.raises(ValueError, match="read_pool=None"):
        await initialize_request_service(
            "memory", db=database, embedding_provider=object(), qdrant_client=object(),
            read_pool=object(), process_pending_bookmarks=False,
        )
    assert module._db is prior
    from genesis.memory.retrieval import HybridRetriever

    with pytest.raises(ValueError, match="read_pool=None"):
        HybridRetriever(db=database, embedding_provider=object(), qdrant_client=object(), read_pool=object())


async def test_health_cannot_bind_different_database(database, monkeypatch):
    isolate_module(monkeypatch, "health")
    with pytest.raises(ValueError, match="supplied RequestDatabase"):
        await initialize_request_service("health", db=database, service=SimpleNamespace(_db=None))


@pytest.mark.parametrize("name", ["memory", "health", "recon", "outreach"])
@pytest.mark.parametrize("metrics", [False, True])
async def test_reinitialization_to_legacy_removes_scoped_boundary(database, monkeypatch, name, metrics):
    module = isolate_module(monkeypatch, name)
    unrelated = [Middleware(), Middleware()]
    for middleware in unrelated:
        module.mcp.add_middleware(middleware)
    dependencies = {}
    if name == "memory":
        dependencies = dict(embedding_provider=object(), qdrant_client=object(), process_pending_bookmarks=False)
    elif name == "health":
        dependencies = dict(service=StandaloneHealthDataService(status_path=database.path.parent / "status.json", db=database))
    elif name == "outreach":
        dependencies = dict(pipeline=object(), engagement=None, config=None)
    server = await initialize_request_service(name, db=database, **dependencies)
    await database.close()
    legacy = await get_db(database.path)
    try:
        if name == "health":
            dependencies["service"] = SimpleNamespace(_db=legacy)
        else:
            dependencies["db"] = legacy
        initializer = module.init if name == "memory" else getattr(module, f"init_{name}_mcp")
        initializer(activity_tracker=Mock() if metrics else None, **dependencies)

        @server.tool()
        async def legacy_read() -> int:
            return (await legacy.execute_fetchall("SELECT 1"))[0][0]

        async with Client(server) as client:
            assert (await client.call_tool("legacy_read")).data == 1
        assert server.middleware[:2] == unrelated
        assert len(server.middleware) == 2 + int(metrics)
    finally:
        await legacy.close()


@pytest.mark.parametrize("wrapper", [False, True])
@pytest.mark.parametrize("runtime", [False, True])
async def test_scoped_health_rejects_unsupported_service_before_mutation(database, monkeypatch, wrapper, runtime):
    from genesis.observability.health_data import HealthDataService

    module = isolate_module(monkeypatch, "health")
    previous = StandaloneHealthDataService(status_path=database.path.parent / "status.json", db=database)
    module.init_health_mcp(previous)
    before = (module._service, module._event_bus, module._activity_tracker, module._job_retry_registry)
    middleware = list(module.mcp.middleware)
    unsupported = HealthDataService(db=database) if runtime else SimpleNamespace(_db=database)
    with pytest.raises(ValueError, match="StandaloneHealthDataService"):
        if wrapper:
            await initialize_request_service("health", db=database, service=unsupported)
        else:
            module.init_health_mcp(unsupported, event_bus=object())
    assert (module._service, module._event_bus, module._activity_tracker, module._job_retry_registry) == before
    assert module.mcp.middleware == middleware


@pytest.mark.parametrize("kind", ["standalone", "runtime"])
@pytest.mark.parametrize("has_db", [False, True])
async def test_legacy_health_implementations_stay_supported(database, monkeypatch, kind, has_db):
    from genesis.observability.health_data import HealthDataService

    module = isolate_module(monkeypatch, "health")
    legacy = await get_db(database.path) if has_db else None
    try:
        service = (HealthDataService(db=legacy) if kind == "runtime" else
                   StandaloneHealthDataService(status_path=database.path.parent / "status.json", db=legacy))
        module.init_health_mcp(service)
        assert module._service is service
        assert module.mcp.middleware == []
    finally:
        if legacy is not None:
            await legacy.close()


@pytest.mark.parametrize("bound_elsewhere", [False, True])
async def test_tracker_mismatch_rejects_without_rebinding(database, monkeypatch, bound_elsewhere):
    module = isolate_module(monkeypatch, "recon")
    await initialize_request_service("recon", db=database)
    previous = (module._db, list(module.mcp.middleware))
    tracker = ProviderActivityTracker()
    original = object() if bound_elsewhere else None
    if original is not None:
        tracker.set_db(original)
    with pytest.raises(ValueError, match="tracker.*supplied RequestDatabase"):
        await initialize_request_service("recon", db=database, activity_tracker=tracker)
    assert tracker._db is original
    assert (module._db, module.mcp.middleware) == previous


async def test_registration_preserves_pending_tracker_flush(database, monkeypatch):
    module = isolate_module(monkeypatch, "recon")
    tracker = ProviderActivityTracker()
    tracker.set_db(database)
    entered, release = asyncio.Event(), asyncio.Event()
    original = tracker._flush_to_db

    async def delayed_flush():
        entered.set()
        await release.wait()
        await original()

    monkeypatch.setattr(tracker, "_flush_to_db", delayed_flush)
    tracker.record("synthetic-pending", latency_ms=3, success=True)
    task = tracker._flush_task
    try:
        await entered.wait()
        for _ in range(2):
            await initialize_request_service("recon", db=database, activity_tracker=tracker)
        assert module.mcp.middleware[0]._tracker is tracker
        assert tracker._db is database and tracker._flush_task is task
        assert tracker.summary("synthetic-pending")["calls"] == 1
    finally:
        release.set()
        await task
    observer = await get_db(database.path)
    try:
        rows = await observer.execute_fetchall("SELECT provider,latency_ms FROM activity_log")
        assert [tuple(row) for row in rows] == [("synthetic-pending", 3)]
    finally:
        await observer.close()
