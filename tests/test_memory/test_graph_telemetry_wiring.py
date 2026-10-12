"""The four production callers open and close a traversal tally correctly.

``test_graph_telemetry.py`` pins the mechanism; these pin the CALL SITES, which
decide how many rows the FalkorDB cutover verdict sees. A fake ``graph_traverse``
reports one traversal through the real ``note_traversal`` path, and the row
writer is replaced by a capture, so each test reads exactly what its caller
would have written.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from genesis.memory import graph_telemetry as telemetry

pytestmark = pytest.mark.asyncio


@pytest.fixture
def written(monkeypatch):
    """Telemetry on, with every row the callers write captured here."""
    monkeypatch.delenv("GENESIS_GRAPH_TELEMETRY_DISABLED", raising=False)
    rows: list[dict] = []

    async def _capture(tally, db, *, db_path=None):
        rows.append({"caller": tally.caller, "traversals": tally.traversals, "db_path": db_path})

    monkeypatch.setattr(telemetry, "_write_tally", _capture)
    return rows


async def _noting_traverse(db, root_id, **kwargs):
    await telemetry.note_traversal(
        db,
        outcome="primary",
        configured="falkordb",
        served_by="falkordb",
        primary_reason=None,
        final_reason=None,
    )
    return SimpleNamespace(nodes=[], query_ms=0.0)


@pytest.fixture
def mcp_state():
    from genesis.mcp import memory_mcp

    memory_mcp.init(db=MagicMock(), qdrant_client=MagicMock(), embedding_provider=MagicMock())
    yield memory_mcp
    memory_mcp._store = None
    memory_mcp._retriever = None
    memory_mcp._user_model_evolver = None
    memory_mcp._db = None
    memory_mcp._qdrant = None


def _result(mid: str):
    from genesis.memory.types import RetrievalResult

    return RetrievalResult(
        memory_id=mid,
        content="c",
        source="test",
        memory_type="episodic",
        score=0.9,
        vector_rank=1,
        fts_rank=1,
        activation_score=0.8,
        payload={},
    )


async def test_memory_recall_writes_one_row_for_all_its_traversals(mcp_state, monkeypatch, written):
    from genesis.mcp.memory import core as core_mod

    monkeypatch.setattr(core_mod, "graph_traverse", _noting_traverse)
    mcp_state._retriever.recall = AsyncMock(
        return_value=[
            _result("11111111-1111-1111-1111-111111111111"),
            _result("22222222-2222-2222-2222-222222222222"),
        ]
    )

    await core_mod.memory_recall.fn(query="q", compact=False, include_graph=True, corrective=False)

    assert written == [{"caller": "recall", "traversals": 2, "db_path": None}]


async def test_memory_recall_still_writes_when_the_request_is_cancelled(
    mcp_state, monkeypatch, written
):
    from genesis.mcp.memory import core as core_mod

    async def _traverse_then_cancel(db, root_id, **kwargs):
        await _noting_traverse(db, root_id, **kwargs)
        raise asyncio.CancelledError

    monkeypatch.setattr(core_mod, "graph_traverse", _traverse_then_cancel)
    mcp_state._retriever.recall = AsyncMock(
        return_value=[
            _result("44444444-4444-4444-4444-444444444444"),
        ]
    )

    with pytest.raises(asyncio.CancelledError):
        await core_mod.memory_recall.fn(
            query="q", compact=False, include_graph=True, corrective=False
        )

    assert written == [{"caller": "recall", "traversals": 1, "db_path": None}]


async def test_memory_expand_still_writes_when_the_request_is_cancelled(
    mcp_state, monkeypatch, written
):
    """A cancelled request must not swallow the traversal it already made: a
    fallback dropped here would read as a clean day."""
    from genesis.mcp.memory import core as core_mod

    async def _traverse_then_cancel(db, root_id, **kwargs):
        await _noting_traverse(db, root_id, **kwargs)
        raise asyncio.CancelledError

    monkeypatch.setattr(core_mod, "graph_traverse", _traverse_then_cancel)
    mid = "33333333-3333-3333-3333-333333333333"
    point = SimpleNamespace(id=mid, payload={"content": "c", "origin_class": "x"})
    mcp_state._qdrant.retrieve = lambda collection_name, ids, with_payload: (
        [point] if collection_name == "episodic_memory" else []
    )

    with pytest.raises(asyncio.CancelledError):
        await core_mod.memory_expand.fn(memory_ids=[mid])

    assert written == [{"caller": "expand", "traversals": 1, "db_path": None}]


async def test_drift_recall_writes_one_drift_row(written):
    from genesis.memory.drift import drift_recall

    embeddings = AsyncMock()
    embeddings.embed = AsyncMock(side_effect=Exception("no embeddings"))
    row = {"memory_id": "m1", "content": "t", "confidence": 0.5, "collection": "episodic_memory"}
    with (
        patch(
            "genesis.memory.drift.memory_crud.search_ranked",
            new_callable=AsyncMock,
            return_value=[{"memory_id": "m1", "content": "t", "rank": -1.0}],
        ),
        patch(
            "genesis.memory.drift._identify_clusters",
            new_callable=AsyncMock,
            return_value=(None, None),
        ),
        patch(
            "genesis.memory.drift.memory_crud.get_by_id",
            new_callable=AsyncMock,
            return_value=row,
        ),
        patch(
            "genesis.memory.drift.graph_traverse",
            new=_noting_traverse,
        ),
        patch(
            "genesis.memory.retrieval._expired_candidate_ids",
            new_callable=AsyncMock,
            return_value=set(),
        ),
    ):
        await drift_recall(
            "q", db=AsyncMock(), qdrant_client=MagicMock(), embedding_provider=embeddings
        )

    assert written == [{"caller": "drift", "traversals": 1, "db_path": None}]


async def test_the_ambient_worker_writes_one_row_through_its_db_path(tmp_path, written):
    """The worker's retrieval connection is read-only, so its row must go through
    ``db_path``; the inner drift tally joins the worker's instead of writing."""
    import sqlite3

    from genesis.session_awareness import worker as worker_mod

    from ..test_session_awareness.conftest import seed_theme

    sessions, state = tmp_path / "s", tmp_path / "sa"
    seed_theme(sessions, "wire-1")
    db_file = tmp_path / "g.db"
    sqlite3.connect(str(db_file)).close()

    async def _ranking_that_traverses(**kwargs):
        async with telemetry.traversal_tally(kwargs["db"], caller="drift"):
            await _noting_traverse(kwargs["db"], "m1")
        return [{"memory_id": "m1", "score": 0.9, "lanes": ["vector"]}]

    with patch.object(worker_mod, "rank_candidates", new=_ranking_that_traverses):
        result = await worker_mod.run_worker(
            "wire-1",
            no_arbiter=True,
            sessions_root=sessions,
            state_root=state,
            db_path=db_file,
            qdrant_url="http://127.0.0.1:1",
        )

    assert result["status"] == "no_arbiter"
    assert written == [{"caller": "ambient", "traversals": 1, "db_path": str(db_file)}]
