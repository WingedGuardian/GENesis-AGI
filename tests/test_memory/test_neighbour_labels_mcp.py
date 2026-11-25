"""Labelled links at the MCP surface: memory_expand and memory_recall (MW-11).

Drives the real tool functions with a real SQLite database and a fake vector
store, so the label pass runs against the queries it ships with. The text
contract and each exclusion are pinned in ``test_neighbour_labels.py``; these
pin the wiring: both builders label, relations read plainly, a credential
neighbour stays unlabelled, and a failed credential read withholds every label.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite
import pytest

from genesis.memory.graphstore import GraphNode

pytestmark = pytest.mark.asyncio

ROOT = "11111111-1111-1111-1111-111111111111"
NEAR = "22222222-2222-2222-2222-222222222222"
FAR = "33333333-3333-3333-3333-333333333333"
CRED = "44444444-4444-4444-4444-444444444444"
TYPED = "55555555-5555-5555-5555-555555555555"
FAKE_SECRET = "fakeMcpLabelSecret-Wr7k"

CONTENT = {
    ROOT: "the root memory",
    NEAR: "Hook budget is anchored to process start",
    FAR: "Deploy guard refuses a dirty tree",
    CRED: f"Router login Value: {FAKE_SECRET}",
    TYPED: "An older ruling this one replaced",
}


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    await conn.execute(
        "CREATE TABLE memory_metadata (memory_id TEXT PRIMARY KEY, invalid_at TEXT, "
        "deprecated INTEGER, origin_class TEXT)"
    )
    await conn.execute(
        "CREATE TABLE knowledge_units (id TEXT, qdrant_id TEXT, project_type TEXT, domain TEXT)"
    )
    for mid in CONTENT:
        await conn.execute("INSERT INTO memory_metadata VALUES (?, NULL, 0, 'first_party')", (mid,))
    await conn.execute(
        "INSERT INTO knowledge_units VALUES ('u', ?, 'reference', 'reference.credentials')",
        (CRED,),
    )
    try:
        yield conn
    finally:
        await conn.close()


@pytest.fixture
def mcp_state(db, monkeypatch):
    from unittest.mock import MagicMock

    from genesis.mcp import memory_mcp
    from genesis.mcp.memory import core as core_mod

    memory_mcp.init(db=db, qdrant_client=MagicMock(), embedding_provider=MagicMock())

    def _retrieve(collection_name, ids, with_payload):
        if collection_name != "episodic_memory":
            return []
        return [SimpleNamespace(id=i, payload={"content": CONTENT[i]}) for i in ids if i in CONTENT]

    memory_mcp._qdrant.retrieve = _retrieve

    async def _traverse(db, root_id, **kwargs):
        return SimpleNamespace(
            nodes=[
                GraphNode(NEAR, "supports", 1, 0.9),
                GraphNode(TYPED, "succeeded_by", 1, 0.8),
                GraphNode(CRED, "extends", 1, 0.7),
                GraphNode(FAR, "", 2, 0.6),
            ],
            query_ms=0.0,
        )

    monkeypatch.setattr(core_mod, "graph_traverse", _traverse)
    yield memory_mcp
    memory_mcp._store = None
    memory_mcp._retriever = None
    memory_mcp._user_model_evolver = None
    memory_mcp._db = None
    memory_mcp._qdrant = None


def _neighbours(results) -> dict[str, dict]:
    (root,) = [r for r in results if r.get("memory_id") == ROOT]
    return {n["memory_id"]: n for n in root["graph_neighbors"]}


def _assert_labelled(by_id):
    assert by_id[NEAR]["label"] == "Hook budget is anchored to process start"
    assert by_id[NEAR]["relation"] == "similar"
    assert by_id[NEAR]["link_type"] == "supports"  # the raw type is kept
    assert by_id[TYPED]["relation"] == "succeeded by"
    assert by_id[TYPED]["label"] == "An older ruling this one replaced"
    assert by_id[FAR]["relation"] == "via linked"  # depth 2, no stored type
    assert by_id[FAR]["label"] == "Deploy guard refuses a dirty tree"
    assert "label" not in by_id[CRED]
    assert by_id[CRED]["relation"] == "similar"
    assert FAKE_SECRET not in repr(by_id)


async def test_memory_expand_labels_its_neighbours(mcp_state):
    from genesis.mcp.memory import core as core_mod

    results = await core_mod.memory_expand(memory_ids=[ROOT])
    _assert_labelled(_neighbours(results))


async def test_memory_recall_labels_its_neighbours(mcp_state):
    from genesis.mcp.memory import core as core_mod
    from genesis.memory.types import RetrievalResult

    mcp_state._retriever.recall = AsyncMock(
        return_value=[
            RetrievalResult(
                memory_id=ROOT,
                content=CONTENT[ROOT],
                source="test",
                memory_type="episodic",
                score=0.9,
                vector_rank=1,
                fts_rank=1,
                activation_score=0.8,
                payload={},
            )
        ]
    )
    results = await core_mod.memory_recall(
        query="q", compact=False, include_graph=True, corrective=False
    )
    _assert_labelled(_neighbours(results))


async def test_a_failed_credential_read_leaves_every_neighbour_unlabelled(mcp_state, db):
    from genesis.mcp.memory import core as core_mod

    await db.execute("DROP TABLE knowledge_units")
    by_id = _neighbours(await core_mod.memory_expand(memory_ids=[ROOT]))
    assert all("label" not in n for n in by_id.values())
    assert by_id[NEAR]["relation"] == "similar"  # relations need no read


@pytest.mark.parametrize(
    ("link_type", "depth", "relation"),
    [
        ("supports", 1, "similar"),
        ("extends", 1, "similar"),
        ("", 1, "linked"),
        (None, 1, "linked"),
        ("contradicts", 1, "contradicts"),
        ("related_to", 2, "via related to"),
        ("extends", 2, "via similar"),
    ],
)
async def test_relation_reads_plainly(link_type, depth, relation):
    from genesis.mcp.memory.core import _relation

    assert _relation(link_type, depth) == relation


# ── the attributable click metric ─────────────────────────────────────────


async def test_an_expand_records_the_expanding_session(mcp_state, monkeypatch):
    from genesis.eval import j9_hooks
    from genesis.mcp.memory import core as core_mod

    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "fake-transcript-0001")
    seen = {}

    async def _emit(db, *, memory_ids, source, session_id=None):
        seen.update(ids=memory_ids, source=source, session_id=session_id)

    monkeypatch.setattr(j9_hooks, "emit_recall_used", _emit)
    await core_mod.memory_expand(memory_ids=[ROOT])
    assert seen == {"ids": [ROOT], "source": "memory_expand", "session_id": "fake-transcript-0001"}


async def test_recall_used_event_carries_its_session_id(db):
    from genesis.eval.j9_hooks import emit_recall_used

    await db.execute(
        "CREATE TABLE eval_events (id TEXT PRIMARY KEY, timestamp TEXT, dimension TEXT, "
        "event_type TEXT, subject_id TEXT, session_id TEXT, metrics_json TEXT, created_at TEXT)"
    )
    await emit_recall_used(db, memory_ids=[ROOT], source="memory_expand", session_id="t-1")
    await emit_recall_used(db, memory_ids=[ROOT], source="memory_expand")
    rows = await db.execute_fetchall("SELECT session_id FROM eval_events ORDER BY rowid")
    assert [r[0] for r in rows] == ["t-1", None]
