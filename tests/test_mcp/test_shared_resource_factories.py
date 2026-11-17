"""Real schema/facade ownership with captured initializer arguments/providers."""

import threading
from types import SimpleNamespace

import pytest

from genesis.db.connection import get_db
from genesis.db.request_database import DatabaseScopeError, RequestDatabase
from genesis.db.schema import create_all_tables
from genesis.mcp.shared_http import SharedHTTPConfig
from genesis.mcp.shared_resources import initialize_shared_service
from genesis.mcp.shared_settings import SharedServiceSettings


def settings(name):
    return SharedServiceSettings(SharedHTTPConfig(name, "http://127.0.0.1:9123/mcp", 1, 1, 2, 30, 4096),
                                 "genesis-mcp-" + name + ".service", 1, 2, 1, 0)


@pytest.fixture
async def factory(tmp_path, monkeypatch):
    import qdrant_client

    from genesis.db import request_database
    from genesis.env import embed_priority_tier
    from genesis.mcp import request_service, shared_http
    from genesis.memory import embeddings, reranker
    from genesis.runtime._core import GenesisRuntime

    path = tmp_path / "factory.db"
    setup = await get_db(path)
    await create_all_tables(setup)
    await setup.commit()
    await setup.close()
    state = SimpleNamespace(events=[], resources=[], managers=[], arguments=[], failure=None,
                            close_failure=None, chains=0, providers=0, tier=embed_priority_tier())
    # These are constructor/argument tests. Exact-stack compatibility runs in
    # the separately qualified environment; production still checks metadata.
    monkeypatch.setattr(shared_http, "require_qualified_stack", lambda: None)
    monkeypatch.setattr(GenesisRuntime, "_instance", None)

    class CapturingDatabase(RequestDatabase):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            state.managers.append(self)

    monkeypatch.setattr(request_database, "RequestDatabase", CapturingDatabase)

    class Resource:
        def __init__(self, name):
            state.events.append("construct-" + name)
            if state.failure == name:
                raise ValueError("synthetic constructor failure")
            self.name, self.closes, self.thread = name, 0, threading.get_ident()
            state.resources.append(self)

        def close(self):
            assert self.thread == threading.get_ident()
            manager = state.managers[0]
            assert manager._closing and not manager._owners and not manager._workers
            self.closes += 1
            state.events.append("close-" + self.name)
            if state.close_failure == self.name:
                raise ValueError("synthetic close failure")

        async def aclose(self):
            self.close()

    class Qdrant(Resource):
        def __init__(self, **kwargs):
            super().__init__("qdrant")

    class Provider(Resource):
        @staticmethod
        def build_chain(**kwargs):
            index = state.chains
            state.chains += 1
            state.arguments.append(("chain", kwargs.copy()))
            result = []
            for suffix in ("a", "b"):
                item = Resource(f"backend-{index}-{suffix}")
                kwargs["on_backend_created"](item)
                result.append(item)
            return result

        def __init__(self, **kwargs):
            index = state.providers
            state.providers += 1
            super().__init__("cache-" + str(index))

        def close_cache(self):
            self.close()

    class Reranker(Resource):
        def __init__(self):
            super().__init__("reranker")

        async def close(self):
            Resource.close(self)

    monkeypatch.setattr(qdrant_client, "QdrantClient", Qdrant)
    monkeypatch.setattr(embeddings, "EmbeddingProvider", Provider)
    monkeypatch.setattr(reranker, "VoyageReranker", Reranker)

    async def initialize(name, *, db, activity_tracker, **kwargs):
        state.events.append("initialize-" + name)
        state.arguments.append(("initialize", (name, db, activity_tracker, kwargs)))
        assert activity_tracker._db is db
        with pytest.raises(DatabaseScopeError):
            await db.execute("SELECT 1")
        if state.failure == "initialize":
            raise ValueError("synthetic constructor failure")
        return SimpleNamespace(service=name)

    monkeypatch.setattr(request_service, "initialize_request_service", initialize)

    async def health_actions(db):
        await db.execute("SELECT 1")
        state.events.append("health-actions")

    def routing():
        state.events.append("routing")

    def enter(name="memory", **overrides):
        return initialize_shared_service(settings(name), db_path=path,
            status_path=tmp_path / "status.json", cache_dir=tmp_path / "cache",
            setup_routing=routing, setup_health_actions=health_actions, **overrides)

    state.enter, state.path = enter, path
    yield state
    for manager in state.managers:
        await manager.close()


@pytest.mark.parametrize("name", ["health", "memory", "outreach", "recon"])
async def test_factory_paths_own_one_facade_and_preserve_standalone_arguments(factory, name):
    async with factory.enter(name) as service:
        assert service.server.service == name
        assert service.database is factory.managers[0]
        assert service.tracker.persist_activity is True
        assert all(resource.closes == 0 for resource in factory.resources)
        entry = next(value for kind, value in factory.arguments if kind == "initialize")
        kwargs = entry[3]
        if name == "health":
            assert kwargs["service"]._db is service.database and "health-actions" in factory.events
        elif name == "memory":
            assert kwargs["read_pool"] is None and kwargs["process_pending_bookmarks"] is False
            assert kwargs["storage_embedding_provider"] is not kwargs["recall_embedding_provider"]
            chains = [value for kind, value in factory.arguments if kind == "chain"]
            assert "priority_tier" not in chains[0] and chains[1]["priority_tier"] is factory.tier
        elif name == "outreach":
            assert kwargs == {"pipeline": None, "engagement": None, "config": None}
        else:
            assert kwargs == {}
    assert len(factory.managers) == 1
    assert all(resource.closes == 1 for resource in factory.resources)
    with pytest.raises(DatabaseScopeError):
        async with service.database.unit_of_work():
            pass


@pytest.mark.parametrize("failure", ["qdrant", "backend-0-a", "backend-0-b", "cache-0",
                                     "backend-1-a", "backend-1-b", "cache-1", "reranker", "initialize"])
async def test_every_constructor_prefix_is_retired_before_startup_failure(factory, failure):
    factory.failure = failure
    with pytest.raises(ValueError, match="synthetic constructor failure"):
        async with factory.enter():
            pytest.fail("Failed startup must not yield a service")
    assert all(resource.closes == 1 for resource in factory.resources)


async def test_first_closer_failure_does_not_skip_other_resources(factory):
    factory.close_failure = "reranker"
    with pytest.raises(ExceptionGroup, match="Shared resource cleanup failed"):
        async with factory.enter():
            pass
    assert all(resource.closes == 1 for resource in factory.resources)
    assert factory.events[-1] == "close-qdrant"


async def test_unconfirmed_drain_withholds_all_provider_closers(factory, monkeypatch):
    manager, original = None, None

    async def fail(*, grace_s):
        raise DatabaseScopeError("synthetic unconfirmed registry")

    try:
        with pytest.raises(DatabaseScopeError, match="unconfirmed registry"):
            async with factory.enter() as service:
                manager, original = service.database, service.database.drain
                monkeypatch.setattr(manager, "drain", fail)
        assert all(resource.closes == 0 for resource in factory.resources)
    finally:
        if manager is not None:
            monkeypatch.setattr(manager, "drain", original)
            await original(grace_s=0)


async def test_missing_database_refuses_before_provider_construction(factory):
    factory.path.unlink()
    with pytest.raises(ValueError, match="existing database"):
        async with factory.enter():
            pytest.fail("Missing database must refuse")
    assert not factory.managers and not factory.resources and factory.events == []


async def test_partial_schema_refuses_before_provider_construction(factory):
    db = await get_db(factory.path)
    await db.execute("DROP TABLE activity_log")
    await db.commit()
    await db.close()
    with pytest.raises(ValueError, match="existing Genesis table schema"):
        async with factory.enter():
            pytest.fail("Partial schema must refuse")
    assert len(factory.managers) == 1 and not factory.resources and factory.events == []
    assert factory.managers[0]._closing and not factory.managers[0]._owners


async def test_bootstrapped_runtime_is_not_borrowed(factory, monkeypatch):
    from genesis.runtime._core import GenesisRuntime

    monkeypatch.setattr(GenesisRuntime, "_instance", SimpleNamespace(_bootstrapped=True))
    with pytest.raises(ValueError, match="standalone process"):
        async with factory.enter():
            pytest.fail("Existing runtime must refuse")
    assert not factory.managers and not factory.resources


async def test_factory_version_fence_precedes_resource_construction(factory, monkeypatch):
    from genesis.mcp import shared_http
    from genesis.mcp.shared_http import _settings

    monkeypatch.setattr(shared_http, "require_qualified_stack", _settings.require_qualified_stack)
    monkeypatch.setattr(_settings, "version", lambda package: "unqualified")
    with pytest.raises(RuntimeError, match="Unqualified shared MCP dependency"):
        async with factory.enter():
            pytest.fail("Unqualified stack must refuse")
    assert not factory.managers and not factory.resources and factory.events == []
