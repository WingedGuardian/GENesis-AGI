"""Actual memory MCP tool and MemoryStore, with owned SQL and fake vector edges."""

import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from genesis.mcp.external_profiles import ExternalProfileMiddleware
from tests.test_mcp.test_validator_verification_writer import recording as recording


@pytest.mark.parametrize("collection", ["episodic_memory", "knowledge_base"])
async def test_memory_store_persists_and_supersedes_through_actual_tool(recording, monkeypatch, collection):
    from genesis.mcp import memory as memory_mcp
    from genesis.memory import entity_anchors, entity_resolution, graph
    from genesis.memory import store as store_mod
    from genesis.memory.store import MemoryStore
    from genesis.perception import confidence

    path, shared, _ = recording
    vectors = {}
    embedding = SimpleNamespace(embed=AsyncMock(return_value=[0.25] * 4), tracker=None)
    qdrant = SimpleNamespace(retrieve=lambda *, collection_name, ids, **kw: [
        SimpleNamespace(id=mid) for mid in ids if (collection_name, mid) in vectors
    ])

    def upsert(client, *, collection, point_id, vector, payload):
        assert client is qdrant
        vectors[collection, point_id] = dict(payload)

    def update(client, *, collection, point_id, payload):
        assert client is qdrant
        vectors[collection, point_id].update(payload)

    monkeypatch.setattr(store_mod, "upsert_point", upsert)
    monkeypatch.setattr(store_mod, "update_payload", update)
    monkeypatch.setattr(entity_resolution, "load_aliases", lambda: {})
    monkeypatch.setattr(confidence, "load_config", lambda: confidence.ConfidenceGatesConfig())
    anchors = AsyncMock()
    monkeypatch.setattr(entity_anchors, "record_anchors", anchors)
    monkeypatch.setattr(graph, "invalidate_graph_cache", lambda: None)
    monkeypatch.setattr(memory_mcp, "_store", MemoryStore(
        embedding_provider=embedding, qdrant_client=qdrant, db=shared,
    ))
    monkeypatch.setattr(memory_mcp, "_db", shared)
    monkeypatch.setattr(memory_mcp, "_retriever", object())
    monkeypatch.setattr(memory_mcp, "_process_pending_bookmarks", False)
    monkeypatch.delenv("GENESIS_SESSION_ORIGIN", raising=False)
    tool = await memory_mcp.mcp.get_tool("memory_store")

    def server(profile):
        mcp = FastMCP("owned-memory")
        mcp.add_tool(tool)
        mcp.add_middleware(ExternalProfileMiddleware("memory", profile))
        return mcp

    arguments = dict(source="owner-directed synthetic measurement", confidence=0.95,
                     memory_class="fact", wing="dev_workflow", room="validation", collection=collection)
    async with Client(server("validator")) as client:
        old = (await client.call_tool("memory_store", arguments | {
            "content": "Synthetic validator measurement: 3 of 4 controls passed",
        })).data
        new = (await client.call_tool("memory_store", arguments | {
            "content": "Synthetic corrected measurement: 4 of 4 controls passed", "supersedes": old,
        })).data
        assert old != new
    # FastMCP 4 runs background tasks only through the tasks extension, which
    # Genesis does not install: task metadata starts no task and still meets the
    # external profile's refusal.
    assert tool.task_config.mode == "forbidden"
    async with Client(server("external")) as client:
        assert await client.list_tools() == []
        with pytest.raises(ToolError, match="unavailable"):
            await client.call_tool("memory_store", arguments | {"content": "Forbidden external write"})
        task_result = await client.call_tool_mcp(
            "memory_store", arguments | {"content": "Must not enter"},
            meta={"modelcontextprotocol.io/task": {"ttl": 60000}},
        )
        assert task_result.is_error is True
        assert "modelcontextprotocol.io/task" not in (task_result.meta or {})
        assert "unavailable" in task_result.content[0].text
    # This reader is a fresh physical SQLite connection, not a mocked store response.
    with sqlite3.connect(path) as fresh:
        fresh.row_factory = sqlite3.Row
        rows = {row["memory_id"]: dict(row) for row in fresh.execute("SELECT * FROM memory_fts")}
        metadata = {row["memory_id"]: dict(row) for row in fresh.execute("SELECT * FROM memory_metadata")}
        assert set(rows) == set(metadata) == {old, new}
        assert rows[new]["content"] == "Synthetic corrected measurement: 4 of 4 controls passed"
        assert rows[new]["collection"] == collection
        assert metadata[old]["deprecated"] == 1 and metadata[old]["superseded_by"] == new
        assert metadata[new]["embedding_status"] == "embedded"
        assert fresh.execute(
            "SELECT COUNT(*) FROM memory_links WHERE source_id=? AND target_id=? AND link_type='succeeded_by'",
            (old, new),
        ).fetchone()[0] == 1
    assert vectors[collection, old]["deprecated"] is True
    assert embedding.embed.await_count == 2
    assert anchors.await_count == 2
    assert all(call.kwargs["db_path"] == str(path) for call in anchors.await_args_list)
