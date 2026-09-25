"""Caller-chosen graph visibility — issue #1896.

``memory_recall(include_deprecated=True)`` returned the deprecated memory it was
asked for and then dropped every one of its ``graph_neighbors``, because the
traversal re-applied a predicate the caller had explicitly opted out of. The
parameter could not reach the store: ``GraphStore.traverse`` had nowhere to put
it, and each backend applied the predicate internally.

Two properties are pinned here, and the FIRST is the one that matters most:

  * with ``include_hidden`` OFF, every backend answers exactly as before — this
    is the hot path and the regression that would actually hurt;
  * with it ON, a hidden memory can be a root AND an intermediate hop, and the
    backends still agree with each other.

Live-graph evidence for the same properties (70 roots x 3 backends, byte-exact
off / 20-of-20 recovered on) is recorded in the PR body; this file is the
deterministic form of it. The FalkorDB store is covered here only at the
binding — that the flag reaches the engine as a query parameter — because an
engine is not available in CI; its semantics were measured against the live
engine (0 rows off, 36 on, for one hidden root).
"""

from __future__ import annotations

import contextlib
import logging

import aiosqlite
import networkx as nx
import pytest

from genesis.memory.graph import _traverse_cte, invalidate_graph_cache
from genesis.memory.graphstore import (
    GraphModeUnsupported,
    GraphUnavailableError,
)
from genesis.memory.graphstore_nx import NetworkxGraphStore, _bfs_with_strength

pytestmark = pytest.mark.asyncio


@contextlib.contextmanager
def caplog_at(level: int):
    """Collect genesis.memory.graph records at `level` or above.

    A plain `caplog` fixture would also capture the loud warning the DEGRADED
    tier legitimately emits in a neighbouring test; this scopes to one logger so
    an assertion about silence means silence from the code under test.
    """
    logger = logging.getLogger("genesis.memory.graph")
    records: list[logging.LogRecord] = []

    class _Sink(logging.Handler):
        def emit(self, record):
            records.append(record)

    sink = _Sink(level=level)
    logger.addHandler(sink)
    previous = logger.level
    logger.setLevel(level)
    try:
        yield records
    finally:
        logger.removeHandler(sink)
        logger.setLevel(previous)


# H is hidden and is deliberately a SHORTCUT: A->H->E is shorter than
# A->C->D->E, so if H ever contaminates the centrality graph, C's and D's
# betweenness MOVE. A centrality test over a hidden node that changed no score
# would pass whether or not the filter worked.
#
# X is the isolated-visible case: visible itself, but its only edge points at H.
# The old loader dropped that row and so never produced X as a node at all; a
# node-induced filter would keep X as an isolated node. MEASURED on the live
# graph: 1,209 such memories.
_LINKS = [
    ("A", "C", "supports", 0.9),
    ("C", "D", "extends", 0.9),
    ("D", "E", "supports", 0.9),
    ("A", "H", "supports", 0.9),
    ("H", "E", "supports", 0.9),
    ("X", "H", "supports", 0.9),
    # DEPTH 2, and it has to be here or a whole clause goes untested. With H
    # reachable at depth 1 from every root that reaches it, the CTE's ANCHOR
    # clauses decide every case and its RECURSIVE-step visibility clause is
    # never the deciding predicate. MEASURED: reverting that clause alone was
    # observable in 0 of 36 (root x flag x depth) combinations of the fixture
    # without W — and DIFFERS with it. The same hole hid the NetworkX walk's
    # deeper-hop behaviour.
    ("C", "W", "supports", 0.9),
    # The BITEMPORAL limb. Every other node is seeded `invalid_at = NULL`, so
    # without this the whole suite exercised only `deprecated != 0` — while four
    # docstrings claim the flag un-hides an EXPIRED memory too. A mutation
    # gating only the deprecated limb passed everywhere.
    ("D", "Q", "supports", 0.9),
]
#: Hidden because superseded.
_DEPRECATED = {"H", "W"}
#: Hidden because bitemporally expired — a DIFFERENT limb of the same predicate,
#: and the one nothing reached before.
_EXPIRED = {"Q"}
_HIDDEN = _DEPRECATED | _EXPIRED
#: Far enough in the past to be expired under any clock this test runs on.
_PAST = "2020-01-01T00:00:00+00:00"


@pytest.fixture
async def vis_db():
    db = await aiosqlite.connect(":memory:")
    await db.execute(
        """CREATE TABLE memory_links (
               source_id TEXT NOT NULL, target_id TEXT NOT NULL,
               link_type TEXT NOT NULL, strength REAL NOT NULL DEFAULT 0.5,
               created_at TEXT NOT NULL,
               PRIMARY KEY (source_id, target_id, link_type))"""
    )
    await db.execute(
        """CREATE TABLE memory_metadata (
               memory_id TEXT PRIMARY KEY, invalid_at TEXT, deprecated INTEGER)"""
    )
    for src, tgt, lt, strength in _LINKS:
        await db.execute(
            "INSERT INTO memory_links VALUES (?, ?, ?, ?, '2026-09-24')",
            (src, tgt, lt, strength),
        )
    for node in {n for pair in _LINKS for n in pair[:2]}:
        # Two INDEPENDENT reasons a memory is hidden, seeded separately so each
        # limb of the predicate is exercised by some root: a non-zero
        # `deprecated`, and an `invalid_at` in the past. A node is never both,
        # or a mutation gating one limb would still be caught by the other.
        await db.execute(
            "INSERT INTO memory_metadata VALUES (?, ?, ?)",
            (
                node,
                _PAST if node in _EXPIRED else None,
                1 if node in _DEPRECATED else 0,
            ),
        )
    await db.commit()
    invalidate_graph_cache()
    yield db
    await db.close()


@pytest.fixture(autouse=True)
def pin_nx_store(monkeypatch):
    """Make the facade's backend selection deterministic for this module.

    `_traversal_store()` reads the install's mode lever, so without this the
    facade tests would exercise whichever backend the machine happens to be
    configured for — and on an install set to `falkordb` with no engine present
    they would pass through a degrade path instead of the one under test. Pinning
    to NetworkX is also the INTERESTING case: it is the backend that declines the
    hidden mode.
    """
    from genesis.memory import graph as graph_mod

    monkeypatch.setattr(graph_mod, "_traversal_store", lambda: graph_mod._store)


async def _ids(store, db, root, *, include_hidden=False, depth=3):
    """Ids from a store DIRECTLY. Visible mode only — the NetworkX store
    declines `include_hidden=True` by design, so a hidden traversal is a facade
    question (`_facade_ids`), not a store one."""
    nodes = await store.traverse(
        db, root, max_depth=depth, min_strength=0.0, include_hidden=include_hidden
    )
    return [n.memory_id for n in nodes]


async def _cte_ids(db, root, *, include_hidden=False, depth=3):
    nodes = await _traverse_cte(db, root, depth, 0.0, include_hidden)
    return [n.memory_id for n in nodes]


async def _facade_ids(db, root, *, include_hidden=False, depth=3):
    """Ids through the PUBLIC facade — the path a caller actually takes, decline
    and re-route included. This is what must be asserted for the hidden mode."""
    from genesis.memory import graph as graph_mod

    result = await graph_mod.traverse(
        db, root, max_depth=depth, min_strength=0.0, include_hidden=include_hidden
    )
    return [n.memory_id for n in result.nodes]


def _oracle_ids(root, *, depth=3, include_hidden):
    """An INDEPENDENT expectation, from the walk over a graph built in the test.

    The withdrawn design gave hidden-mode parity between the walk and the CTE for
    free, because production held an unfiltered graph. It no longer does — so the
    parity claim is kept as an ORACLE instead: build the graph here, run the real
    `_bfs_with_strength`, and require the CTE's hidden answer to match it. That is
    a stronger check than comparing the facade to the CTE, which after the
    re-route would be comparing the CTE to itself.
    """
    g = nx.MultiDiGraph()
    for src, tgt, lt, strength in _LINKS:
        if include_hidden or (src not in _HIDDEN and tgt not in _HIDDEN):
            g.add_edge(src, tgt, key=lt, link_type=lt, strength=strength)
    if root not in g:
        return []
    return [n.memory_id for n in _bfs_with_strength(g, root, max_depth=depth, min_strength=0.0)]


# ── the default: nothing changes ──────────────────────────────────────────


async def test_hidden_root_still_yields_nothing_by_default(vis_db):
    """The pre-existing behaviour, pinned. This is NOT the defect — it is the
    correct default, and the one a careless fix would break."""
    store = NetworkxGraphStore()
    assert await _ids(store, vis_db, "H") == []
    assert await _cte_ids(vis_db, "H") == []


async def test_hidden_node_is_not_reachable_by_default(vis_db):
    """A visible root must not be handed a hidden neighbour."""
    store = NetworkxGraphStore()
    assert "H" not in await _ids(store, vis_db, "A")
    assert "H" not in await _cte_ids(vis_db, "A")


async def test_default_traversal_does_not_pass_through_a_hidden_node(vis_db):
    """E is reachable from A only via D (visible) or H (hidden). Both routes
    exist, so this asserts the hidden route is not *taken* — not merely that
    the endpoint is filtered out of the result.

    Guard-the-guard: the assertion would hold vacuously if E were unreachable
    altogether, so the visible route is asserted present in the same breath.
    """
    store = NetworkxGraphStore()
    for got in (await _ids(store, vis_db, "A"), await _cte_ids(vis_db, "A")):
        assert "E" in got, "the visible route A->C->D->E must exist"
        assert "H" not in got

    # Reached at depth 3 via D, never at depth 2 via H — asserted for BOTH
    # backends. Hoisted out of the loop above deliberately: inside it, this
    # re-queried the NetworkX store twice and the CTE never got the depth-2
    # check at all, so the test read as covering two backends while covering
    # one.
    assert "E" not in await _ids(store, vis_db, "A", depth=2), (
        "E at depth 2 means the NetworkX walk crossed H"
    )
    assert "E" not in await _cte_ids(vis_db, "A", depth=2), (
        "E at depth 2 means the CTE walk crossed H"
    )


async def test_include_hidden_defaults_to_false(vis_db):
    """Passing nothing must behave exactly as passing False — the entire
    existing call graph relies on it (drift.py, dream centrality, the compact
    recall path)."""
    store = NetworkxGraphStore()
    assert await _ids(store, vis_db, "A") == await _ids(store, vis_db, "A", include_hidden=False)
    assert await _cte_ids(vis_db, "A") == await _cte_ids(vis_db, "A", include_hidden=False)


# ── the fix ───────────────────────────────────────────────────────────────


async def test_hidden_root_returns_its_neighbours_when_asked(vis_db):
    """THE defect (#1896): the caller asked for the deprecated memory, so its
    neighbours must come back too."""
    assert await _facade_ids(vis_db, "H", include_hidden=True) == ["E"]
    assert await _cte_ids(vis_db, "H", include_hidden=True) == ["E"]
    # Independent expectation, not a second reading of the same engine.
    assert _oracle_ids("H", include_hidden=True) == ["E"]


async def test_hidden_neighbour_appears_when_asked(vis_db):
    """Distinct from the root question, and the issue says so explicitly: this
    is about a hidden neighbour of a VISIBLE root."""
    assert "H" in await _facade_ids(vis_db, "A", include_hidden=True)
    assert "H" in await _cte_ids(vis_db, "A", include_hidden=True)
    assert "H" in _oracle_ids("A", include_hidden=True)


async def test_traversal_passes_through_a_hidden_node_when_asked(vis_db):
    """The pass-through leg of the equivalence argument, in the other
    direction: with the flag on, E is reachable at depth 2 via H."""
    from genesis.memory import graph as graph_mod

    result = await graph_mod.traverse(
        vis_db, "A", max_depth=2, min_strength=0.0, include_hidden=True
    )
    by_id = {n.memory_id: n for n in result.nodes}
    assert "E" in by_id and by_id["E"].depth == 2, "E at depth 2 requires crossing H"
    cte = await _traverse_cte(vis_db, "A", 2, 0.0, True)
    assert {n.memory_id for n in cte} >= {"H", "E"}
    assert "E" in _oracle_ids("A", depth=2, include_hidden=True)


async def test_flag_on_never_loses_a_node_the_default_showed(vis_db):
    """Un-hiding is strictly additive. A fix that reached hidden nodes by
    RESTRUCTURING the walk could satisfy every test above and still drop a
    visible neighbour."""
    store = NetworkxGraphStore()
    for root in ("A", "C", "D", "X", "W", "Q"):
        off = set(await _ids(store, vis_db, root))
        on = set(await _facade_ids(vis_db, root, include_hidden=True))
        assert off <= on, f"{root}: {off - on} disappeared when un-hiding"


# ── cross-backend parity: which backend answers must not matter ───────────


@pytest.mark.parametrize("include_hidden", [False, True])
@pytest.mark.parametrize("root", ["A", "C", "D", "E", "H", "X", "W", "Q"])
async def test_backends_agree_field_for_field(vis_db, root, include_hidden):
    cte = await _traverse_cte(vis_db, root, 3, 0.0, include_hidden)
    cte_ids = [n.memory_id for n in cte]

    if not include_hidden:
        # Two real engines answering the same question, field for field.
        store = NetworkxGraphStore()
        walk = await store.traverse(
            vis_db, root, max_depth=3, min_strength=0.0, include_hidden=False
        )

        def shape(ns):
            return [(n.memory_id, n.link_type, n.depth, n.strength) for n in ns]

        assert shape(walk) == shape(cte)
    else:
        # The NetworkX store declines this mode, so there is no second engine to
        # compare against — the ORACLE stands in, which is stronger than
        # comparing the facade (which re-routes here) to the CTE it routes to.
        assert cte_ids == _oracle_ids(root, include_hidden=True), (
            "the CTE's hidden-mode answer diverges from the walk's semantics"
        )
        assert await _facade_ids(vis_db, root, include_hidden=True) == cte_ids, (
            "the facade did not route this mode to the CTE"
        )


# ── centrality: the second path, and the one that feeds the shield ────────


async def test_the_default_projection_is_filtered_exactly_as_before(vis_db):
    """`_ensure_graph(db)` — every pre-#1896 caller's call — must still return
    the VISIBLE projection, node for node and edge for edge.

    This is the invariant the second-projection shape buys, and the reason it
    was chosen over carrying one unfiltered graph plus a filtered view for
    centrality: that view cost +62.3 MiB, 6.1s per rebuild and a 1.60x
    betweenness slowdown on every install (MEASURED on the live graph), and its
    node iteration order moved the shield's sampled scores under a fixed seed.
    """
    store = NetworkxGraphStore()
    visible = await store._ensure_graph(vis_db)

    old = nx.MultiDiGraph()
    for src, tgt, lt, strength in _LINKS:
        if src not in _HIDDEN and tgt not in _HIDDEN:
            old.add_edge(src, tgt, key=lt, link_type=lt, strength=strength)

    assert set(visible.nodes()) == set(old.nodes())
    assert set(visible.edges(keys=True)) == set(old.edges(keys=True))
    assert all(visible[u][v][k] == old[u][v][k] for u, v, k in old.edges(keys=True))
    # A CONCRETE graph, not a filtered view — that distinction is the whole
    # measured point above, and `edge_subgraph`/`subgraph` return view classes.
    assert type(visible) is nx.MultiDiGraph

    # NEGATIVE CONTROL: X is visible, its only edge points at H, so the filter
    # must drop it entirely. A node-induced filter would keep it as an isolated
    # node — MEASURED at 1,209 such memories live, which is what made the
    # rejected view shape subtle as well as slow.
    assert "X" not in visible.nodes(), (
        "X survived — the loader is not dropping rows by EITHER endpoint"
    )
    assert "X" in {n for pair in _LINKS for n in pair[:2]}, (
        "fixture no longer contains the isolated-visible case"
    )


async def test_centrality_scores_are_unchanged_by_the_visibility_move(vis_db):
    """Exact betweenness, not a sampled approximation: the graph is under the
    store's 200-node threshold, so ``k`` is None and the result is
    deterministic. A sampled comparison here would be unseeded and flaky.

    H is a SHORTCUT (A->H->E beats A->C->D->E), so contamination would move C's
    and D's scores rather than merely appending zeros — which is what makes this
    an assertion and not a formality.

    Coverage limit, stated because the test cannot carry it: at production scale
    ``k=min(200, n)`` and the sample is UNSEEDED, so this pins the exact path
    only. The production path is covered by the graph-identity assertion in
    ``test_the_default_projection_is_filtered_exactly_as_before``, not here.
    """
    store = NetworkxGraphStore()
    ranked = await store.centrality(vis_db, None)

    old = nx.MultiDiGraph()
    for src, tgt, lt, strength in _LINKS:
        if src not in _HIDDEN and tgt not in _HIDDEN:
            old.add_edge(src, tgt, key=lt, link_type=lt, strength=strength)
    assert old.number_of_nodes() <= 200, "fixture must stay on the exact path"
    expected = sorted(
        nx.betweenness_centrality(old, k=None).items(),
        key=lambda kv: kv[1],
        reverse=True,
    )

    assert ranked == expected
    assert {n for n, _ in ranked} == {"A", "C", "D", "E"}
    # Guard-the-guard: the ranking must be non-trivial, or "unchanged" is empty.
    assert any(score > 0 for _, score in ranked), (
        "every score is 0 — this fixture cannot detect contamination"
    )


async def test_centrality_is_not_reachable_by_the_hidden_flag(vis_db):
    """There is deliberately NO visibility parameter on ``centrality``. The
    importance shield is not a caller with an audit question, and adding one
    would let a traversal's opt-in change which memories survive
    consolidation."""
    import inspect

    sig = inspect.signature(NetworkxGraphStore.centrality)
    assert "include_hidden" not in sig.parameters


# ── the callers: the flag has to actually be threaded ─────────────────────
#
# Everything above proves the stores HONOUR the flag. These prove the two MCP
# surfaces PASS it — which is the defect as reported. Written as behavioural
# tests driving the real tool functions, not as an AST check for the keyword: a
# call site can contain `include_hidden=...` and still be unreachable, and a
# keyword can be present and bound to the wrong thing.


@pytest.fixture
async def labelled_db():
    """A real database for the label tests, closed UNCONDITIONALLY.

    Deliberately a fixture rather than an open/close pair inside the test body.
    An `await conn.close()` on the last line does not run when an assertion above
    it fails, and an unclosed aiosqlite connection holds a live THREAD — which
    blocks event-loop teardown, so the run HANGS instead of reporting the
    failure. Measured while verify-RED was mutating this file: a mutation that
    should have produced a clean red produced a 180s timeout.
    """
    db = await aiosqlite.connect(":memory:")
    await db.execute(
        """CREATE TABLE memory_metadata (
               memory_id TEXT PRIMARY KEY, invalid_at TEXT, deprecated INTEGER)"""
    )
    try:
        yield db
    finally:
        await db.close()


@pytest.fixture
def _mcp_state():
    from unittest.mock import MagicMock

    from genesis.mcp import memory_mcp

    memory_mcp.init(db=MagicMock(), qdrant_client=MagicMock(), embedding_provider=MagicMock())
    yield memory_mcp
    memory_mcp._store = None
    memory_mcp._retriever = None
    memory_mcp._user_model_evolver = None
    memory_mcp._db = None
    memory_mcp._qdrant = None


class _TraverseRecorder:
    """Stands in for graph_traverse and records the visibility argument."""

    def __init__(self):
        self.calls: list[dict] = []

    async def __call__(self, db, root_id, **kwargs):
        self.calls.append({"root_id": root_id, **kwargs})

        class _R:
            nodes: list = []
            query_ms = 0.0

        return _R()


@pytest.mark.parametrize("asked", [False, True])
async def test_memory_recall_threads_the_visibility_choice(_mcp_state, monkeypatch, asked):
    from unittest.mock import AsyncMock

    from genesis.mcp.memory import core as core_mod
    from genesis.memory.types import RetrievalResult

    rec = _TraverseRecorder()
    monkeypatch.setattr(core_mod, "graph_traverse", rec)
    _mcp_state._retriever.recall = AsyncMock(
        return_value=[
            RetrievalResult(
                memory_id="11111111-1111-1111-1111-111111111111",
                content="c",
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

    await core_mod.memory_recall.fn(
        query="q",
        include_deprecated=asked,
        compact=False,
        include_graph=True,
        corrective=False,
    )

    assert rec.calls, "graph enrichment never ran — this test proves nothing"
    assert rec.calls[0]["include_hidden"] is asked


@pytest.mark.parametrize("asked", [False, True])
async def test_memory_expand_threads_the_visibility_choice(_mcp_state, monkeypatch, asked):
    from types import SimpleNamespace

    from genesis.mcp.memory import core as core_mod

    rec = _TraverseRecorder()
    monkeypatch.setattr(core_mod, "graph_traverse", rec)
    mid = "22222222-2222-2222-2222-222222222222"
    point = SimpleNamespace(id=mid, payload={"content": "c", "origin_class": "x"})
    _mcp_state._qdrant.retrieve = lambda collection_name, ids, with_payload: (
        [point] if collection_name == "episodic_memory" else []
    )

    await core_mod.memory_expand.fn(memory_ids=[mid], include_deprecated=asked)

    assert rec.calls, "graph enrichment never ran — this test proves nothing"
    assert rec.calls[0]["include_hidden"] is asked


# ── both limbs of the predicate, not just the deprecated one ──────────────


@pytest.mark.parametrize("backend", ["nx", "cte"])
async def test_an_EXPIRED_memory_is_hidden_by_default_and_shown_when_asked(vis_db, backend):
    """`include_hidden` is named for VISIBILITY, not for deprecation, and the
    predicate has two independent limbs: a non-zero `deprecated` and an
    `invalid_at` in the past. Q is expired and NOT deprecated, so it reaches the
    second limb only.

    Nothing exercised this before: every fixture row was seeded
    `invalid_at = NULL`, so a change gating only the deprecated limb passed the
    entire suite while four docstrings claimed otherwise.
    """

    async def run(root, *, include_hidden):
        if backend == "nx":
            # Via the facade: the store declines this mode and the facade
            # re-routes it, which is the behaviour a caller actually sees.
            return await _facade_ids(vis_db, root, include_hidden=include_hidden)
        return await _cte_ids(vis_db, root, include_hidden=include_hidden)

    # Guard-the-guard: Q must really be expired-and-not-deprecated, or this test
    # is just another deprecation test wearing a different name.
    cur = await vis_db.execute(
        "SELECT invalid_at, deprecated FROM memory_metadata WHERE memory_id='Q'"
    )
    invalid_at, deprecated = await cur.fetchone()
    assert invalid_at == _PAST and not deprecated, (
        f"fixture does not isolate the bitemporal limb: {invalid_at=} {deprecated=}"
    )

    assert "Q" not in await run("D", include_hidden=False), (
        "an expired memory must be hidden by default"
    )
    assert "Q" in await run("D", include_hidden=True), (
        "include_hidden must un-hide an EXPIRED memory, not only a deprecated one"
    )
    # And it is a root in its own right, symmetrically with the deprecated case.
    assert await run("Q", include_hidden=False) == []


@pytest.mark.parametrize("backend", ["nx", "cte"])
async def test_a_hidden_node_at_DEPTH_2_is_gated(vis_db, backend):
    """W is hidden and sits at depth 2 from A (A->C->W), which is the only way to
    exercise the CTE's RECURSIVE-step visibility clause — its anchor clauses
    decide every depth-1 case. Reverting that clause was observable in 0 of 36
    fixture combinations before W existed.
    """

    async def run(root, *, include_hidden, depth=3):
        if backend == "nx":
            return await _facade_ids(vis_db, root, include_hidden=include_hidden, depth=depth)
        return await _cte_ids(vis_db, root, include_hidden=include_hidden, depth=depth)

    # Guard-the-guard: W must be at depth 2 from A and depth 1 from C.
    assert "W" in await run("C", include_hidden=True, depth=1), (
        "fixture moved: W is no longer a direct neighbour of C"
    )
    assert "W" not in await run("A", include_hidden=True, depth=1), (
        "fixture moved: W is reachable from A at depth 1, so this is not a depth-2 case at all"
    )

    assert "W" not in await run("A", include_hidden=False), (
        "a hidden node at depth 2 leaked with the flag off — the recursive-step "
        "visibility clause is not binding"
    )
    assert "W" in await run("A", include_hidden=True)


# ── the NetworkX store DECLINES this mode, and the facade routes it ───────


async def test_the_networkx_store_declines_hidden_traversal(vis_db):
    """It raises rather than serving the mode, and raises the NARROW type.

    Declining is the seam's own idiom — FalkorDB raises for `centrality` because
    it has no betweenness — but the type matters: a bare
    ``GraphUnavailableError`` would make the facade log a dead-engine warning
    with a traceback on a healthy, expected path, which trains readers to ignore
    the warning that means something.
    """
    store = NetworkxGraphStore()
    with pytest.raises(GraphModeUnsupported):
        await store.traverse(vis_db, "H", max_depth=2, min_strength=0.0, include_hidden=True)
    # Still a GraphUnavailableError, so every existing handler keeps working.
    assert issubclass(GraphModeUnsupported, GraphUnavailableError)


async def test_declining_is_cheap_and_builds_no_projection(vis_db):
    """The decline must happen BEFORE any graph work. A store that built a
    projection and then refused would pay the whole cost for nothing — and the
    withdrawn design's cost is exactly what this route exists to avoid."""
    store = NetworkxGraphStore()
    with pytest.raises(GraphModeUnsupported):
        await store.traverse(vis_db, "H", max_depth=2, min_strength=0.0, include_hidden=True)
    assert store._graph is None, "the store built a projection on its way to declining"


async def test_the_facade_routes_a_declined_mode_to_the_CTE(vis_db, monkeypatch):
    """END TO END through the public facade: the NetworkX store declines and the
    caller still gets the hidden neighbours, from the SQL tier.

    This is the property that makes the decline safe — without it, declining
    would just be the #1896 symptom with a different cause.
    """
    from genesis.memory import graph as graph_mod

    monkeypatch.setattr(graph_mod, "_traversal_store", lambda: graph_mod._store)

    hidden_off = await graph_mod.traverse(vis_db, "H", max_depth=3, min_strength=0.0)
    hidden_on = await graph_mod.traverse(
        vis_db, "H", max_depth=3, min_strength=0.0, include_hidden=True
    )
    assert [n.memory_id for n in hidden_off.nodes] == [], (
        "the default must still hide a hidden root"
    )
    assert [n.memory_id for n in hidden_on.nodes] == ["E"], (
        "the facade did not route the declined mode to the SQL tier"
    )


async def test_the_facade_does_not_log_a_warning_for_a_declined_mode(vis_db, monkeypatch, caplog):
    """A decline is not a degradation. If it logs at WARNING with a traceback,
    the genuine dead-engine warning becomes noise a reader learns to skip."""
    import logging

    from genesis.memory import graph as graph_mod

    monkeypatch.setattr(graph_mod, "_traversal_store", lambda: graph_mod._store)
    with caplog.at_level(logging.WARNING, logger="genesis.memory.graph"):
        result = await graph_mod.traverse(
            vis_db, "H", max_depth=3, min_strength=0.0, include_hidden=True
        )
    assert [n.memory_id for n in result.nodes] == ["E"], (
        "guard-the-guard: the call must have actually taken the decline path"
    )
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING], (
        f"declining logged at WARNING: {[r.getMessage() for r in caplog.records]}"
    )


# ── the hidden neighbours are LABELLED, not merely returned ───────────────


class _TwoNodeTraversal:
    """A traversal result carrying one hidden and one visible neighbour."""

    def __init__(self, hidden_id, visible_id):
        from genesis.memory.graphstore import GraphNode

        self.nodes = [
            GraphNode(memory_id=hidden_id, link_type="supports", depth=1, strength=0.9),
            GraphNode(memory_id=visible_id, link_type="supports", depth=1, strength=0.8),
        ]
        self.query_ms = 0.0


@pytest.mark.parametrize("tool", ["memory_recall", "memory_expand"])
async def test_a_hidden_neighbour_is_LABELLED_when_the_caller_opted_in(
    _mcp_state, monkeypatch, tool, labelled_db
):
    """Un-hiding without labelling hands a caller superseded ids that look live.

    The flag is per-CALL, not per-root, so it also un-hides neighbours of VISIBLE
    memories — MEASURED on the live graph: 11,032 of 73,823 visible memories with
    out-edges (14.9%) gain at least one hidden neighbour at depth 1 alone. A
    caller following a ``[→ related: id:…]`` hint would otherwise have no way to
    tell a superseded neighbour from a live one, and the documented next step is
    to expand it.

    ``hidden`` is present ONLY on a hidden neighbour, so its ABSENCE means
    visible — asserted in both directions here, because a label that is always
    present or always absent carries no information.
    """
    from unittest.mock import AsyncMock

    from genesis.mcp.memory import core as core_mod
    from genesis.memory.types import RetrievalResult

    hidden_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    visible_id = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    root_id = "cccccccc-cccc-cccc-cccc-cccccccccccc"

    async def _fake_traverse(db, rid, **kw):
        return _TwoNodeTraversal(hidden_id, visible_id)

    monkeypatch.setattr(core_mod, "graph_traverse", _fake_traverse)
    # A REAL database, and `_hidden_neighbour_ids` is NOT mocked. An earlier
    # version of this test monkeypatched it, which made the test grade a mock —
    # so deleting the real label logic entirely left it green. A mutation sweep
    # caught that. The lesson: the one function under test is the one thing a
    # test must never stand in for.
    await labelled_db.executemany(
        "INSERT INTO memory_metadata VALUES (?, NULL, ?)",
        [(hidden_id, 1), (visible_id, 0), (root_id, 0)],
    )
    await labelled_db.commit()
    monkeypatch.setattr(_mcp_state, "_db", labelled_db)

    async def run(*, include_deprecated):
        if tool == "memory_recall":
            _mcp_state._retriever.recall = AsyncMock(
                return_value=[
                    RetrievalResult(
                        memory_id=root_id,
                        content="c",
                        source="t",
                        memory_type="episodic",
                        score=0.9,
                        vector_rank=1,
                        fts_rank=1,
                        activation_score=0.8,
                        payload={},
                    )
                ]
            )
            out = await core_mod.memory_recall.fn(
                query="q",
                include_deprecated=include_deprecated,
                compact=False,
                include_graph=True,
                corrective=False,
            )
        else:
            from types import SimpleNamespace

            point = SimpleNamespace(id=root_id, payload={"content": "c", "origin_class": "x"})
            _mcp_state._qdrant.retrieve = lambda collection_name, ids, with_payload: (
                [point] if collection_name == "episodic_memory" else []
            )
            out = await core_mod.memory_expand.fn(
                memory_ids=[root_id],
                include_deprecated=include_deprecated,
            )
        return {n["memory_id"]: n for n in out[0]["graph_neighbors"]}

    by_id = await run(include_deprecated=True)
    assert by_id[hidden_id].get("hidden") is True, (
        "a hidden neighbour came back unlabelled — indistinguishable from a live one"
    )
    assert "hidden" not in by_id[visible_id], (
        "a VISIBLE neighbour was labelled hidden, so the label means nothing"
    )

    off = await run(include_deprecated=False)
    assert not any("hidden" in n for n in off.values()), (
        "the label appeared with the flag off, where no hidden neighbour can exist"
    )


async def test_the_label_lookup_is_skipped_entirely_when_not_opted_in(_mcp_state):
    """With the predicate applied, no hidden id can be in the result — so the
    lookup is a guaranteed-useless query on the hot path and must not run.

    `db=None` is the proof: it would raise if anything touched the database.
    """
    from genesis.mcp.memory import core as core_mod

    results = [{"graph_neighbors": [{"memory_id": "x"}]}]
    assert await core_mod._label_hidden_neighbours(None, results, include_hidden=False) is True
    assert "hidden" not in results[0]["graph_neighbors"][0]


async def test_no_query_runs_when_there_are_no_neighbours_to_label(_mcp_state):
    """The other free case: the flag is ON but nothing came back. `memory_expand`
    defaults the flag on, so this is its every-miss path — it must not pay a
    query for an empty candidate set."""
    from genesis.mcp.memory import core as core_mod

    for empty in ([], [{"graph_neighbors": []}], [{"graph_neighbors": None}], [{}]):
        assert await core_mod._label_hidden_neighbours(None, empty, include_hidden=True) is True, (
            f"a query was attempted for {empty!r}"
        )


async def test_the_label_fails_open_but_REPORTS_that_it_could_not_check(_mcp_state, caplog):
    """A label is an enrichment: failing to compute one must not cost the caller
    their neighbours. But it must not silently assert the opposite of the truth
    either.

    Returning "nothing is hidden" for a call that could not look would mark every
    neighbour live — MEASURED, 4,276 hidden ids on one live install — in a payload
    whose documented next step is to expand and trust them. So the contract is:
    neighbours survive, the return value is False, and it logs at WARNING rather
    than DEBUG, because the caller explicitly asked to see hidden memories and
    this is a failure of that request.
    """
    import logging

    from genesis.mcp.memory import core as core_mod

    class _Boom:
        async def execute(self, *a, **k):
            raise RuntimeError("database is gone")

    results = [{"graph_neighbors": [{"memory_id": "x"}, {"memory_id": "y"}]}]
    with caplog.at_level(logging.WARNING, logger="genesis.mcp.memory.core"):
        ok = await core_mod._label_hidden_neighbours(_Boom(), results, include_hidden=True)
    assert ok is False, "a failed check must report itself, not return silently"
    assert len(results[0]["graph_neighbors"]) == 2, (
        "fail-open broken: the caller lost their neighbours"
    )
    assert not any("hidden" in n for n in results[0]["graph_neighbors"]), (
        "labels were invented on a call that could not check"
    )
    assert [r for r in caplog.records if r.levelno >= logging.WARNING], (
        "the failure was logged below WARNING, where production will not see it"
    )


@pytest.mark.parametrize("tool", ["memory_recall", "memory_expand"])
async def test_a_failed_label_check_withdraws_the_absence_contract(_mcp_state, monkeypatch, tool):
    """END TO END: when labelling cannot run, the response must not let absence be
    read as "visible". Both tools mark it.

    Without this, a caller sees five unlabelled neighbours and cannot tell "all
    five are live" from "nobody checked" — and the safe-sounding reading is the
    wrong one.
    """
    from unittest.mock import AsyncMock

    from genesis.mcp.memory import core as core_mod
    from genesis.memory.types import RetrievalResult

    root_id = "dddddddd-dddd-dddd-dddd-dddddddddddd"

    async def _fake_traverse(db, rid, **kw):
        return _TwoNodeTraversal("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee", root_id)

    monkeypatch.setattr(core_mod, "graph_traverse", _fake_traverse)
    monkeypatch.setattr(core_mod, "_label_hidden_neighbours", AsyncMock(return_value=False))

    if tool == "memory_recall":
        _mcp_state._retriever.recall = AsyncMock(
            return_value=[
                RetrievalResult(
                    memory_id=root_id,
                    content="c",
                    source="t",
                    memory_type="episodic",
                    score=0.9,
                    vector_rank=1,
                    fts_rank=1,
                    activation_score=0.8,
                    payload={},
                )
            ]
        )
        out = await core_mod.memory_recall.fn(
            query="q",
            include_deprecated=True,
            compact=False,
            include_graph=True,
            corrective=False,
        )
    else:
        from types import SimpleNamespace

        point = SimpleNamespace(id=root_id, payload={"content": "c", "origin_class": "x"})
        _mcp_state._qdrant.retrieve = lambda collection_name, ids, with_payload: (
            [point] if collection_name == "episodic_memory" else []
        )
        out = await core_mod.memory_expand.fn(memory_ids=[root_id], include_deprecated=True)

    assert out[0].get("graph_neighbors"), (
        "guard-the-guard: there must be neighbours for the marker to apply to"
    )
    assert out[0].get("graph_neighbors_visibility") == "unknown", (
        "a response whose labels could not be computed did not say so"
    )


async def test_the_two_tools_defaults_are_deliberately_different(_mcp_state):
    """`memory_recall` defaults to hiding, `memory_expand` to showing, and the
    asymmetry is a decision rather than drift — so it is pinned here instead of
    being left to a reader.

    `memory_recall` is a SEARCH: enrichment must not surface what the search
    beside it hides. `memory_expand` is an explicit id lookup that already
    returns the named memory whatever its state, so suppressing only its
    neighbours made the tool asymmetric with itself — and it is the surface the
    proactive hook points callers at, where nothing signals that a handle is
    deprecated.

    This test existed, was deleted by accident during a restructure, and was
    restored only because a mutation sweep noticed that nothing failed when the
    default flipped back. A deleted test is invisible; a green sweep cell is not.
    """
    import inspect

    from genesis.mcp.memory import core as core_mod

    recall = inspect.signature(core_mod.memory_recall.fn).parameters
    expand = inspect.signature(core_mod.memory_expand.fn).parameters
    assert recall["include_deprecated"].default is False, (
        "memory_recall must keep hiding by default — graph enrichment must not "
        "surface what the search beside it hides"
    )
    assert expand["include_deprecated"].default is True, (
        "memory_expand must default to showing neighbours: it already returns "
        "the deprecated memory itself, so hiding only its edges is incoherent"
    )


# ── the DEGRADED hidden path: primary down AND the caller asked ───────────
#
# This pair was 0/106 covered. A line tracer over the whole suite showed the
# inner handler's `except GraphModeUnsupported:` CLAUSE hit twice (CPython traces
# a clause on every match ATTEMPT) and its BODY zero times — which is why the gap
# was invisible to an ordinary coverage read. Deleting the handler left every
# test green while production silently returned to #1896 whenever the primary
# store was unavailable.


class _DeadPrimary:
    """A reachable-but-failing primary store, for the degraded tier."""

    name = "dead-primary"

    def __init__(self):
        self.calls: list[bool] = []

    async def traverse(self, db, root_id, *, max_depth, min_strength, include_hidden=False):
        self.calls.append(include_hidden)
        raise GraphUnavailableError("the engine is not answering")

    async def centrality(self, db, top_n):
        raise GraphUnavailableError("the engine is not answering")

    def invalidate(self) -> None:
        pass


async def test_primary_down_AND_hidden_asked_still_reaches_the_CTE(vis_db, monkeypatch):
    """The composition of two conditions, neither of which is new on its own.

    Primary unavailable takes the loud degrade path; that path tries the
    NetworkX tier, which DECLINES the hidden mode; the decline is caught and the
    SQL tier answers.

    WHAT THE INNER HANDLER IS ACTUALLY FOR — established by mutation, and it is
    NOT what it looks like. Removing it does NOT break this path: the sibling
    ``except GraphUnavailableError`` catches the decline anyway, because
    ``GraphModeUnsupported`` subclasses it, and the CTE still answers correctly.
    MEASURED: with the handler removed, all six tests in this area passed. A
    review had reported its removal as a silent return to #1896; that is refuted.

    Its real property is QUIETNESS. Without it the decline is logged as a SECOND
    dead-engine warning, with a traceback, about a healthy tier, on top of the one
    genuine warning for the primary — which is the noise the narrow type exists to
    remove. So this asserts the warning COUNT as well as the result. Asserting
    only the result would leave the handler's sole real effect untested, which is
    how it sat at zero coverage while looking load-bearing.
    """
    import logging

    from genesis.memory import graph as graph_mod

    dead = _DeadPrimary()
    monkeypatch.setattr(graph_mod, "_traversal_store", lambda: dead)

    with caplog_at(logging.WARNING) as records:
        result = await graph_mod.traverse(
            vis_db, "H", max_depth=3, min_strength=0.0, include_hidden=True
        )
    assert dead.calls == [True], "the primary was not tried first, so this is not the degraded path"
    assert [n.memory_id for n in result.nodes] == ["E"], (
        "primary-down + hidden did not reach the CTE"
    )
    assert len(records) == 1, (
        "expected exactly ONE warning (the genuinely dead primary); got "
        f"{len(records)}: {[r.getMessage()[:70] for r in records]} — a second one "
        "means the decline is being reported as an engine failure"
    )
    assert "dead-primary" in records[0].getMessage(), (
        "the one warning should be about the primary, not about the decline"
    )

    # And the default mode on the same degraded path still hides.
    dead.calls.clear()
    off = await graph_mod.traverse(vis_db, "H", max_depth=3, min_strength=0.0)
    assert [n.memory_id for n in off.nodes] == []
    assert dead.calls == [False]


async def test_a_declined_mode_whose_CTE_then_fails_raises_the_SEAM_type(vis_db, monkeypatch):
    """The facade promises `GraphUnavailableError`, never a raw error.

    The guard that enforces it used to live inside ONE fallback handler's body,
    and an exception raised in an `except` clause is not caught by that clause's
    siblings — so adding a second fallback path re-opened the leak. MEASURED
    before the fix: a decline whose CTE failed raised a bare
    `ValueError: no active connection` at the caller, while the older path
    correctly raised `GraphUnavailableError`. Both paths now share one helper.
    """
    from genesis.memory import graph as graph_mod

    async def _boom(*a, **k):
        raise ValueError("no active connection")

    monkeypatch.setattr(graph_mod, "_traversal_store", lambda: graph_mod._store)
    monkeypatch.setattr(graph_mod, "_traverse_cte", _boom)

    # The DECLINE path (NetworkX active, hidden asked).
    with pytest.raises(GraphUnavailableError) as declined:
        await graph_mod.traverse(vis_db, "H", max_depth=3, min_strength=0.0, include_hidden=True)
    assert "both failed" in str(declined.value), (
        "the decline path leaked a raw error instead of the seam's type"
    )

    # And the pre-existing UNAVAILABLE path, so the parity is asserted rather
    # than assumed — the two must not drift apart again.
    #
    # BOTH tiers have to fail to reach the CTE here. With only the primary dead,
    # the facade tries the NetworkX tier next and it SUCCEEDS for a visible root,
    # so the CTE is never called and nothing raises — which is correct behaviour
    # and made an earlier version of this assertion fail for the wrong reason.
    dead = _DeadPrimary()
    monkeypatch.setattr(graph_mod, "_traversal_store", lambda: dead)
    monkeypatch.setattr(graph_mod, "_store", _DeadPrimary())
    with pytest.raises(GraphUnavailableError) as unavailable:
        await graph_mod.traverse(vis_db, "A", max_depth=3, min_strength=0.0)
    assert "both failed" in str(unavailable.value)
    assert dead.calls == [False], "guard-the-guard: the primary must have been tried on this path"


async def test_the_mode_is_declined_even_without_networkx(vis_db, monkeypatch):
    """Importability is irrelevant to whether this store can serve the mode.

    Checking `_NX_AVAILABLE` first was measured to send a no-NetworkX install
    down the LOUD degrade path for this mode — a dead-engine warning with a
    traceback, once per traversal and so up to five per `memory_recall` — which
    is the noise the narrow type exists to remove.
    """
    import logging

    from genesis.memory import graph as graph_mod
    from genesis.memory import graphstore_nx as nx_mod

    monkeypatch.setattr(graph_mod, "_traversal_store", lambda: graph_mod._store)
    monkeypatch.setattr(nx_mod, "_NX_AVAILABLE", False)

    store = NetworkxGraphStore()
    with pytest.raises(GraphModeUnsupported):
        await store.traverse(vis_db, "H", max_depth=3, min_strength=0.0, include_hidden=True)

    with caplog_at(logging.WARNING) as records:
        result = await graph_mod.traverse(
            vis_db, "H", max_depth=3, min_strength=0.0, include_hidden=True
        )
    assert [n.memory_id for n in result.nodes] == ["E"], (
        "guard-the-guard: the answer must still be correct"
    )
    assert not records, (
        f"declining logged at WARNING on a no-NetworkX install: {[r.getMessage() for r in records]}"
    )
