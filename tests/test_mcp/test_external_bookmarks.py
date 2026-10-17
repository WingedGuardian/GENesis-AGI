"""External tool calls must leave pending Claude bookmarks for Claude."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from genesis.mcp import memory


@pytest.fixture
def pending_memory(monkeypatch, tmp_path):
    pending = tmp_path / "plan_bookmark_pending.json"
    pending.write_text('{"session_id_hint":"fixture-session","title":"Fixture"}')
    monkeypatch.setattr(memory, "_PLAN_BOOKMARK_PENDING", pending)
    for name in ("_store", "_retriever", "_db", "_qdrant", "_bookmark_mgr",
                 "_user_model_evolver", "_process_pending_bookmarks"):
        monkeypatch.setattr(memory, name, getattr(memory, name))
    # Constructors are isolated: no real database, vector service, or embedding API.
    for module, name in (
        ("genesis.bookmark.manager", "BookmarkManager"),
        ("genesis.memory.linker", "MemoryLinker"),
        ("genesis.memory.retrieval", "HybridRetriever"),
        ("genesis.memory.store", "MemoryStore"),
        ("genesis.memory.user_model", "UserModelEvolver"),
    ):
        monkeypatch.setattr(f"{module}.{name}", MagicMock())
    tasks = []

    def capture(coro, **kwargs):
        tasks.append(kwargs["name"])
        coro.close()

    monkeypatch.setattr("genesis.util.tasks.tracked_task", capture)
    return pending, tasks


@pytest.mark.parametrize("external", [True, False])
@pytest.mark.parametrize("tool_name", ["memory_store", "memory_expand"])
async def test_tool_calls_respect_initialization_policy(pending_memory, external, tool_name):
    pending, tasks = pending_memory
    memory.init(
        db=MagicMock(), qdrant_client=MagicMock(), embedding_provider=MagicMock(),
        process_pending_bookmarks=not external,
    )
    memory._store.store = AsyncMock(return_value="fixture-memory")
    memory._qdrant.retrieve.return_value = []
    tools = await memory.mcp.get_tools()
    if tool_name == "memory_store":
        assert await tools[tool_name].fn(content="fixture", source="fixture") == "fixture-memory"
    else:
        assert await tools[tool_name].fn(memory_ids=[]) == [{"not_found": []}]
    assert tasks == ([] if external else ["plan-bookmark-pending"])


def test_default_init_preserves_claude_processing_and_resets_external_policy(pending_memory):
    _, tasks = pending_memory
    dependencies = dict(db=MagicMock(), qdrant_client=MagicMock(), embedding_provider=MagicMock())
    memory.init(**dependencies, process_pending_bookmarks=False)
    memory._require_init()
    assert tasks == []
    memory.init(**dependencies)
    memory._require_init()
    assert tasks == ["plan-bookmark-pending"]


@pytest.mark.parametrize("external", [False, True])
@pytest.mark.parametrize("profile", [None, "external", "validator", "interactive"])
async def test_standalone_bootstrap_passes_policy_to_memory_init(
    monkeypatch, tmp_path, external, profile,
):
    import scripts.genesis_mcp_server as server

    database = tmp_path / "fixture.db"
    database.touch()
    monkeypatch.setattr(server, "_DEFAULT_DB", database)
    monkeypatch.setattr(server, "clear_mcp_crash", MagicMock())
    monkeypatch.setattr(server, "_run_mcp", MagicMock())
    monkeypatch.setattr("genesis.env.recall_read_pool_off", lambda: True)
    monkeypatch.setattr("genesis.db.connection.get_db", AsyncMock(return_value=AsyncMock()))
    monkeypatch.setattr("genesis.routing.standalone.create_standalone_router", MagicMock())
    monkeypatch.setattr("qdrant_client.QdrantClient", MagicMock())
    monkeypatch.setattr("genesis.memory.embeddings.EmbeddingProvider", MagicMock())
    monkeypatch.setattr("genesis.memory.reranker.VoyageReranker", MagicMock())
    monkeypatch.setattr("genesis.observability.provider_activity.ProviderActivityTracker", MagicMock())
    init = MagicMock()
    monkeypatch.setattr(memory, "init", init)
    monkeypatch.setattr(memory.mcp, "_lifespan", memory.mcp._lifespan)
    monkeypatch.setattr(memory.mcp, "middleware", [])
    server._bootstrap_memory(
        {}, process_pending_bookmarks=not external, external_profile=profile,
    )
    async with memory.mcp._lifespan(memory.mcp):
        assert init.call_args.kwargs["process_pending_bookmarks"] is (not external and not profile)
        if profile:
            from genesis.mcp.external_profiles import ExternalProfileMiddleware

            assert isinstance(memory.mcp.middleware[0], ExternalProfileMiddleware)


@pytest.mark.parametrize("external", [False, True])
@pytest.mark.parametrize("server_name", ["memory", "health"])
def test_standalone_entrypoint_selects_external_policy(monkeypatch, tmp_path, external, server_name):
    import scripts.genesis_mcp_server as server

    monkeypatch.setattr("genesis.routing.standalone._external_secret_blocked_keys", None)
    monkeypatch.setattr("genesis.env.secrets_path", lambda: tmp_path / "absent")
    monkeypatch.setattr(server, "is_genesis_enabled", lambda: True)
    # Record and restore this process-global setting; main() calls setdefault.
    monkeypatch.setenv("GENESIS_DB_BUSY_TIMEOUT_MS", "15000")
    monkeypatch.setattr(server.logging, "basicConfig", MagicMock())
    monkeypatch.setattr("genesis.observability.mcp_spawn_identity.capture_spawn_identity", MagicMock())
    bootstrap = MagicMock()
    monkeypatch.setitem(server._BOOTSTRAPPERS, server_name, bootstrap)
    argv = ["--server", server_name]
    if external:
        argv.append("--external-client")
    server.main(argv)
    expected = {"external_profile": "external"} if external else {}
    assert bootstrap.call_args.kwargs == expected
