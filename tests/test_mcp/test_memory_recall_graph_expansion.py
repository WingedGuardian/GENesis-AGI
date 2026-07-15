"""Graph expansion at the MCP recall surfaces (compact / full / proactive).

The graph_expansion module's own behavior (config layering, primitive,
maybe_expand modes) is covered in ``tests/test_memory/test_graph_expansion.py``.
This file pins the WIRING: each surface merges neighbors at the right point so
they flow through the existing provenance/label/wrap/count/drop machinery —
an expanded neighbor must be indistinguishable from an organic result to
WS-3's defenses.

Harness follows ``test_memory_mcp.py``: real in-memory DB (``db`` fixture),
mocked retriever returning canned RetrievalResults, module state swapped on
``genesis.mcp.memory_mcp``. Config redirected into tmp dirs so tests control
the expansion mode (the repo ships ``mode: shadow``).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from genesis.db.crud import memory as memory_crud
from genesis.db.crud import memory_links as memory_links_crud
from genesis.mcp.memory_mcp import mcp
from genesis.memory import graph_expansion
from genesis.memory.types import RetrievalResult
from genesis.security import immunity_shadow


async def _get_tools():
    return await mcp.get_tools()


@pytest.fixture
def expansion_config(tmp_path, monkeypatch):
    """Redirect memory_recall config into tmp dirs; write via returned fn."""
    repo_dir = tmp_path / "repo"
    (repo_dir / "config").mkdir(parents=True)
    user_dir = tmp_path / "user_config"
    user_dir.mkdir()
    monkeypatch.setattr(graph_expansion, "repo_root", lambda: repo_dir)
    monkeypatch.setattr("genesis._config_overlay._user_config_dir", lambda: user_dir)

    def _set(mode: str, **kwargs) -> None:
        (repo_dir / "config" / "memory_recall.yaml").write_text(
            yaml.safe_dump({"graph_expansion": {"mode": mode, **kwargs}}),
        )

    return _set


@pytest.fixture
def record_spy(monkeypatch):
    """Capture immunity_shadow.record_would_block calls (core.py namespace)."""
    calls: list[dict] = []

    async def _spy(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(immunity_shadow, "record_would_block", _spy)
    return calls


def _rr(
    mid: str, *, collection: str = "episodic_memory", origin_class: str | None = "owner"
) -> RetrievalResult:
    return RetrievalResult(
        memory_id=mid,
        content=f"content of {mid}",
        source="memory",
        memory_type=collection,
        score=0.9,
        vector_rank=1,
        fts_rank=None,
        activation_score=0.2,
        payload={"tags": []},
        source_pipeline="hybrid",
        origin_class=origin_class,
        collection=collection,
    )


async def _seed_neighbor(
    db,
    seed_id: str,
    nbr_id: str,
    *,
    strength: float = 0.8,
    collection: str = "episodic_memory",
    origin_class: str | None = "owner",
    tags: str = "",
) -> None:
    """DB rows for the SEED (links only resolve via memory_links) + neighbor."""
    for mid, coll, oc in (
        (seed_id, "episodic_memory", "owner"),
        (nbr_id, collection, origin_class),
    ):
        exists = await memory_crud.get_by_id(db, mid)
        if not exists:
            await memory_crud.create(
                db,
                memory_id=mid,
                content=f"content of {mid}",
                tags=tags if mid == nbr_id else "",
                collection=coll,
            )
            await memory_crud.create_metadata(
                db,
                memory_id=mid,
                created_at="2026-07-01T00:00:00Z",
                collection=coll,
                origin_class=oc,
            )
    await memory_links_crud.create(
        db,
        source_id=seed_id,
        target_id=nbr_id,
        link_type="supports",
        strength=strength,
        created_at="2026-07-01T00:00:00Z",
    )


class _SwappedState:
    """Context manager swapping memory_mcp module state (store/db/retriever)."""

    def __init__(self, db, results: list[RetrievalResult]):
        import genesis.mcp.memory_mcp as mod

        self.mod = mod
        self.db = db
        self.results = results

    def __enter__(self):
        mod = self.mod
        self.old = (mod._store, mod._db, mod._retriever, mod._qdrant)
        retriever = AsyncMock()

        async def _recall(*_a, event_id_sink=None, **_k):
            return list(self.results)

        retriever.recall = _recall
        retriever._embeddings = MagicMock()
        mod._store = MagicMock()
        mod._db = self.db
        mod._retriever = retriever
        mod._qdrant = MagicMock()
        return mod

    def __exit__(self, *exc):
        (self.mod._store, self.mod._db, self.mod._retriever, self.mod._qdrant) = self.old
        return False


# ─── compact surface ─────────────────────────────────────────────────────────


async def test_compact_live_appends_neighbor_preview_with_via_graph(
    db,
    expansion_config,
    record_spy,
):
    expansion_config("live")
    await _seed_neighbor(db, "seed-1", "nbr-1")
    with _SwappedState(db, [_rr("seed-1")]):
        tools = await _get_tools()
        out = await tools["memory_recall"].fn(query="q", compact=True)

    assert [d["memory_id"] for d in out] == ["seed-1", "nbr-1"]
    assert out[1]["via_graph"] is True
    assert "via_graph" not in out[0]
    assert out[1]["preview"].startswith("content of nbr-1")


async def test_compact_live_counts_external_neighbor_blockable(
    db,
    expansion_config,
    record_spy,
):
    expansion_config("live")
    await _seed_neighbor(
        db,
        "seed-1",
        "nbr-ext",
        collection="knowledge_base",
        origin_class="external_untrusted",
    )
    with _SwappedState(db, [_rr("seed-1")]):
        tools = await _get_tools()
        await tools["memory_recall"].fn(query="q", compact=True)

    compact_calls = [c for c in record_spy if c.get("detail") == {"path": "compact"}]
    assert len(compact_calls) == 1
    assert compact_calls[0]["blockable_count"] == 1  # the neighbor, not the seed


async def test_compact_shadow_output_unchanged(db, expansion_config, record_spy):
    expansion_config("shadow")
    await _seed_neighbor(db, "seed-1", "nbr-1")
    with _SwappedState(db, [_rr("seed-1")]):
        tools = await _get_tools()
        out = await tools["memory_recall"].fn(query="q", compact=True)

    assert [d["memory_id"] for d in out] == ["seed-1"]
    rows = await db.execute_fetchall(
        "SELECT event_type FROM eval_events WHERE event_type LIKE 'graph_expansion_%'",
    )
    assert [r[0] for r in rows] == ["graph_expansion_shadow"]


# ─── full surface ────────────────────────────────────────────────────────────


async def test_full_live_merges_neighbor_dict_wrapped_and_counted(
    db,
    expansion_config,
    record_spy,
):
    expansion_config("live")
    await _seed_neighbor(
        db,
        "seed-1",
        "nbr-ext",
        collection="knowledge_base",
        origin_class="external_untrusted",
    )
    with _SwappedState(db, [_rr("seed-1")]):
        tools = await _get_tools()
        out = await tools["memory_recall"].fn(
            query="q",
            compact=False,
            include_graph=False,
            corrective=False,
        )

    by_id = {d["memory_id"]: d for d in out}
    assert set(by_id) == {"seed-1", "nbr-ext"}
    nbr = by_id["nbr-ext"]
    assert nbr["via_graph"] is True
    # went through label_result_dicts (provenance) AND the wrap loop
    assert nbr.get("provenance")
    assert nbr["content"] != "content of nbr-ext"  # wrapped external
    assert "content of nbr-ext" in nbr["content"]
    full_calls = [c for c in record_spy if c.get("detail") is None]
    assert len(full_calls) == 1
    assert full_calls[0]["blockable_count"] == 1


async def test_full_shadow_output_unchanged_and_emits(db, expansion_config, record_spy):
    expansion_config("shadow")
    await _seed_neighbor(db, "seed-1", "nbr-1")
    with _SwappedState(db, [_rr("seed-1")]):
        tools = await _get_tools()
        out = await tools["memory_recall"].fn(
            query="q",
            compact=False,
            include_graph=False,
            corrective=False,
        )

    assert [d["memory_id"] for d in out] == ["seed-1"]
    rows = await db.execute_fetchall(
        "SELECT event_type FROM eval_events WHERE event_type LIKE 'graph_expansion_%'",
    )
    assert [r[0] for r in rows] == ["graph_expansion_shadow"]


async def test_full_off_no_expansion_no_event(db, expansion_config, record_spy):
    expansion_config("off")
    await _seed_neighbor(db, "seed-1", "nbr-1")
    with _SwappedState(db, [_rr("seed-1")]):
        tools = await _get_tools()
        out = await tools["memory_recall"].fn(
            query="q",
            compact=False,
            include_graph=False,
            corrective=False,
        )

    assert [d["memory_id"] for d in out] == ["seed-1"]
    rows = await db.execute_fetchall(
        "SELECT event_type FROM eval_events WHERE event_type LIKE 'graph_expansion_%'",
    )
    assert rows == []


# ─── proactive surface ───────────────────────────────────────────────────────


async def test_proactive_live_appends_defended_neighbor(
    db,
    expansion_config,
    record_spy,
):
    expansion_config("live")
    await _seed_neighbor(
        db,
        "seed-1",
        "nbr-ext",
        collection="knowledge_base",
        origin_class="external_untrusted",
    )
    with _SwappedState(db, [_rr("seed-1")]):
        tools = await _get_tools()
        out = await tools["memory_proactive"].fn(current_message="hello")

    assert [d["memory_id"] for d in out] == ["seed-1", "nbr-ext"]
    nbr = out[1]
    assert nbr["via_graph"] is True
    assert nbr["content"] != "content of nbr-ext"  # wrapped external
    assert "content of nbr-ext" in nbr["content"]
    assert len(record_spy) == 1
    assert record_spy[0]["blockable_count"] == 1  # neighbor counted


async def test_proactive_live_respects_proactive_cap(db, expansion_config, record_spy):
    expansion_config("live", proactive_max_neighbors=2)
    for i, s in enumerate((0.9, 0.8, 0.7)):
        await _seed_neighbor(db, "seed-1", f"nbr-{i}", strength=s)
    with _SwappedState(db, [_rr("seed-1")]):
        tools = await _get_tools()
        out = await tools["memory_proactive"].fn(current_message="hello")

    assert [d["memory_id"] for d in out] == ["seed-1", "nbr-0", "nbr-1"]


async def test_proactive_neighbor_memory_operation_tag_filtered(
    db,
    expansion_config,
    record_spy,
):
    """The organic pool filters memory_operation-tagged items — a neighbor
    carrying that tag (string form, from the FTS row) must be filtered too."""
    expansion_config("live")
    await _seed_neighbor(db, "seed-1", "nbr-memop", tags="memory_operation,foo")
    with _SwappedState(db, [_rr("seed-1")]):
        tools = await _get_tools()
        out = await tools["memory_proactive"].fn(current_message="hello")

    assert [d["memory_id"] for d in out] == ["seed-1"]


async def test_proactive_neighbor_runs_enforce_drop(
    db,
    expansion_config,
    record_spy,
    monkeypatch,
):
    """A neighbor the enforce branch would drop must be dropped (and counted
    in enforced_drops), exactly like an organic item — expansion must not be
    a bypass around the gate-4 pushed-surface cut."""
    expansion_config("live")
    await _seed_neighbor(
        db,
        "seed-1",
        "nbr-ext",
        collection="knowledge_base",
        origin_class="external_untrusted",
    )

    def _drop_external(**kwargs):
        return kwargs.get("origin_class") == "external_untrusted"

    monkeypatch.setattr(immunity_shadow, "should_enforce_drop", _drop_external)
    with _SwappedState(db, [_rr("seed-1")]):
        tools = await _get_tools()
        out = await tools["memory_proactive"].fn(current_message="hello")

    assert [d["memory_id"] for d in out] == ["seed-1"]
    assert record_spy[0]["detail"] == {"enforced_drops": 1}


async def test_proactive_shadow_output_unchanged_and_emits(db, expansion_config, record_spy):
    expansion_config("shadow")
    await _seed_neighbor(db, "seed-1", "nbr-1")
    with _SwappedState(db, [_rr("seed-1")]):
        tools = await _get_tools()
        out = await tools["memory_proactive"].fn(current_message="hello")

    assert [d["memory_id"] for d in out] == ["seed-1"]
    rows = await db.execute_fetchall(
        "SELECT event_type FROM eval_events WHERE event_type LIKE 'graph_expansion_%'",
    )
    assert [r[0] for r in rows] == ["graph_expansion_shadow"]
