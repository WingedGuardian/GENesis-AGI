"""Actual memory tool bodies and telemetry, with only providers replaced."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from genesis.db.crud import procedural
from genesis.mcp.request_service import initialize_request_service
from genesis.observability.provider_activity import ProviderActivityTracker
from tests.test_mcp.test_request_service_initialization import (
    database as database,
)
from tests.test_mcp.test_request_service_initialization import (
    isolate_module,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def memory_service(database, monkeypatch):
    from genesis.mcp.memory import procedural as tools

    memory = isolate_module(monkeypatch, "memory")
    tracker = ProviderActivityTracker()
    tracker.set_db(database)
    server = await initialize_request_service(
        "memory", db=database, activity_tracker=tracker,
        embedding_provider=SimpleNamespace(embed=AsyncMock(return_value=[.1, .2])),
        qdrant_client=object(), process_pending_bookmarks=False,
    )
    server.add_tool(tools.procedure_recall)
    async with database.unit_of_work():
        await procedural.create(
            database, id="synthetic-procedure", task_type="synthetic-task",
            principle="Check the result", steps=["Read", "Verify"], tools_used=[],
            context_tags=["synthetic"], created_at="2026-01-01", draft=0,
            activation_tier="ACTIVE", confidence=.9, success_count=2,
        )
    yield server, memory, tracker
    while database._jobs:
        await asyncio.gather(*tuple(database._jobs))


async def test_actual_procedure_counters_activity_and_embedding_heartbeat(database, memory_service):
    server, memory, tracker = memory_service

    @server.tool()
    async def embed_probe() -> bool:
        _, available = await memory._retriever._embed_query("synthetic")
        return available

    async with Client(server) as first, Client(server) as second:
        results = await asyncio.gather(
            first.call_tool("procedure_recall", {"task_description": "synthetic-task", "context_tags": ["synthetic"]}),
            second.call_tool("procedure_recall", {"task_description": "synthetic-task", "context_tags": ["synthetic"]}),
        )
        assert all(result.structured_content["result"][0]["procedure_id"] == "synthetic-procedure" for result in results)
        assert (await first.call_tool("embed_probe")).data is True
    while database._jobs:
        await asyncio.gather(*tuple(database._jobs))
    async with database.unit_of_work():
        row = await procedural.get_by_id(database, "synthetic-procedure")
        assert row["invocation_count"] == 2
        events = await database.execute_fetchall("SELECT * FROM eval_events WHERE event_type='procedure_invoked'")
        assert len(events) == 2
        activity = await database.execute_fetchall("SELECT * FROM activity_log WHERE provider='mcp.memory.procedure_recall'")
        assert len(activity) == 2
        heartbeat = await database.execute_fetchall("SELECT * FROM call_site_last_run WHERE call_site_id='21b_query_embedding'")
        assert len(heartbeat) == 1
    assert tracker.summary("mcp.memory.procedure_recall")["calls"] == 2


async def test_pending_bookmark_body_persists_on_its_own_unit(database, memory_service, monkeypatch, tmp_path):
    from genesis.bookmark.manager import BookmarkManager
    from genesis.mcp.memory import _plan_bookmark

    server, memory, _ = memory_service
    pending = tmp_path / ".genesis" / "plan_bookmark_pending.json"
    pending.parent.mkdir()
    pending.write_text(json.dumps({"title": "Synthetic plan", "session_id_hint": "11111111-1111-1111-1111-111111111111"}))
    monkeypatch.setattr(_plan_bookmark, "Path", SimpleNamespace(home=lambda: tmp_path))
    monkeypatch.setattr(memory, "_PLAN_BOOKMARK_PENDING", pending)
    monkeypatch.setattr(memory, "_process_pending_bookmarks", True)
    manager = BookmarkManager(
        memory_store=SimpleNamespace(store=AsyncMock(return_value="synthetic-memory")),
        hybrid_retriever=None, db=database,
    )
    # Extraction/provider behavior is not part of the SQLite ownership test.
    monkeypatch.setattr(manager, "_trigger_extraction", AsyncMock())
    monkeypatch.setattr(memory, "_bookmark_mgr", manager)
    async with Client(server) as client:
        await client.call_tool("procedure_recall", {"task_description": "synthetic-task"})
    while database._jobs:
        await asyncio.gather(*tuple(database._jobs))
    async with database.unit_of_work():
        rows = await database.execute_fetchall("SELECT topic,source FROM session_bookmarks")
    assert [(row[0], row[1]) for row in rows] == [("Synthetic plan", "plan")]
    assert not pending.exists()


async def test_real_graph_telemetry_preserves_explicit_commit_prefix(database, memory_service, monkeypatch, tmp_path):
    from genesis.memory import graph, graph_telemetry
    from genesis.memory.graphstore_nx import NetworkxGraphStore

    server, _, _ = memory_service
    store = NetworkxGraphStore()
    monkeypatch.setattr(graph, "_store", store)
    monkeypatch.setattr(graph, "_traversal_store", lambda: store)
    monkeypatch.delenv("GENESIS_GRAPH_TELEMETRY_DISABLED", raising=False)
    monkeypatch.setattr(graph_telemetry, "lost_writes_path", lambda: tmp_path / "lost.jsonl")

    @server.tool()
    async def graph_then_fail() -> str:
        await database.execute(
            "INSERT INTO memory_links(source_id,target_id,link_type,strength,created_at) "
            "VALUES ('root','node-10','supports',.8,'2026-01-01')"
        )
        result = await graph.traverse(database, "root")
        assert [node.memory_id for node in result.nodes] == ["node-10"]
        await database.execute(
            "INSERT INTO memory_links(source_id,target_id,link_type,strength,created_at) "
            "VALUES ('node-10','node-1','supports',.8,'2026-01-01')"
        )
        raise ValueError("after telemetry commit")

    async with Client(server) as client:
        with pytest.raises(ToolError, match="after telemetry commit"):
            await client.call_tool("graph_then_fail")
    async with database.unit_of_work():
        rows = await database.execute_fetchall("SELECT target_id FROM memory_links")
        events = await database.execute_fetchall("SELECT * FROM eval_events WHERE event_type='graph_traverse'")
    assert [row[0] for row in rows] == ["node-10"]
    assert len(events) == 1
    assert store._graph is None
    assert not (tmp_path / "lost.jsonl").exists()
