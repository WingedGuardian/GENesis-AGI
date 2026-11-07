"""Request-scoped graph traversal uses SQL without a process-wide projection."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from genesis.db.connection import get_db
from genesis.db.request_database import RequestDatabase
from genesis.db.schema._tables import TABLES
from genesis.memory import graph, graphstore_nx
from genesis.memory.graphstore import GraphModeUnsupported, GraphNode, GraphUnavailableError
from genesis.memory.graphstore_nx import NetworkxGraphStore

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def scoped_graph(tmp_path, monkeypatch):
    path = tmp_path / "graph.db"
    setup = await get_db(path)
    await setup.execute(TABLES["memory_links"])
    await setup.execute(TABLES["memory_metadata"])
    await setup.executescript("""
        CREATE INDEX graph_source ON memory_links(source_id);
        INSERT INTO memory_links (source_id,target_id,link_type,strength,created_at)
        VALUES ('root', 'first', 'supports', .8, '2026-01-01');
    """)
    await setup.commit()
    await setup.close()
    store = NetworkxGraphStore()
    monkeypatch.setattr(graph, "_store", store)
    monkeypatch.setattr(graph, "_traversal_store", lambda: store)
    # Keep telemetry's filesystem/output effects outside this graph-selection
    # test; integration exercises the real writer with its full schema.
    telemetry = AsyncMock()
    monkeypatch.setattr(graph, "note_traversal", telemetry)
    db = RequestDatabase(path, max_units=2)
    try:
        yield db, store, telemetry
    finally:
        await db.close()


@pytest.mark.parametrize("available", [True, False])
async def test_scoped_networkx_declines_before_projection(scoped_graph, monkeypatch, available):
    db, store, _ = scoped_graph
    monkeypatch.setattr(graphstore_nx, "_NX_AVAILABLE", available)
    async with db.unit_of_work():
        with pytest.raises(GraphModeUnsupported):
            await store.traverse(db, "root", max_depth=2, min_strength=0)
    assert store._graph is None


async def test_fresh_units_and_own_pending_writes_are_visible(scoped_graph):
    db, store, telemetry = scoped_graph
    async with db.unit_of_work():
        first = await graph.traverse(db, "root", max_depth=2)
        assert [node.memory_id for node in first.nodes] == ["first"]
    async with db.unit_of_work():
        await db.execute(
            "INSERT INTO memory_links (source_id,target_id,link_type,strength,created_at) "
            "VALUES ('root', 'second', 'supports', .9, '2026-01-01')"
        )
        second = await graph.traverse(db, "root", max_depth=2)
        assert [node.memory_id for node in second.nodes] == ["second", "first"]
    assert store._graph is None
    assert telemetry.call_args.kwargs["served_by"] == "cte"


@pytest.mark.parametrize("unavailable", [False, True])
async def test_selected_falkor_preserved_with_scoped_sql_fallback(scoped_graph, monkeypatch, unavailable):
    db, store, telemetry = scoped_graph
    projected = [GraphNode(memory_id="projected", link_type="supports", depth=1, strength=.7)]
    traverse = AsyncMock(return_value=projected)
    if unavailable:
        traverse.side_effect = GraphUnavailableError("synthetic unavailable engine")
    primary = SimpleNamespace(name="falkordb", traverse=traverse)
    monkeypatch.setattr(graph, "_traversal_store", lambda: primary)
    async with db.unit_of_work():
        result = await graph.traverse(db, "root", max_depth=2)
    assert [n.memory_id for n in result.nodes] == (["first"] if unavailable else ["projected"])
    traverse.assert_awaited_once()
    assert store._graph is None
    # Existing telemetry records a broken primary before awaiting fallback.
    assert telemetry.call_args.kwargs["served_by"] == ("pending" if unavailable else "falkordb")
    assert telemetry.call_args.kwargs["outcome"] == ("fallback" if unavailable else "primary")
