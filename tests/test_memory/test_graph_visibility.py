"""Caller-chosen graph visibility — issue #1896.

``memory_recall(include_deprecated=True)`` returned the deprecated memory it was
asked for and then dropped every one of its ``graph_neighbors``, because the
traversal re-applied a predicate the caller had explicitly opted out of. The
parameter could not reach the store: ``GraphStore.traverse`` had nowhere to put
it, and each backend applied the predicate internally.

Two properties are pinned here, and the FIRST is the one that matters most:

  * with ``include_deprecated`` OFF, every backend answers exactly as before — this
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
    # without this the whole suite exercised only `deprecated != 0`, and a
    # mutation gating just that limb passed everywhere. Q is what makes the
    # limbs' DIFFERENT widths observable: expiry hides at every flag value,
    # deprecation only at the default. Adding this row is what surfaced the
    # PR #2339 finding — the flag was un-hiding both.
    ("D", "Q", "supports", 0.9),
    # Q needs an OUT-EDGE or the "not traversable as a ROOT" assertions are
    # VACUOUS: with no row whose `source_id` is Q, `traverse("Q")` returns []
    # because there is nothing to walk, and the anchor-SOURCE expiry clause is
    # never the deciding predicate. MEASURED (PR #2339 review): neutralising that
    # clause was invisible across all 8 roots x 2 flag values without this row,
    # and is RED with it. Exactly the hole W was added to close for depth, in the
    # other limb. E is already visible, so no oracle expectation moves.
    ("Q", "E", "supports", 0.9),
]
#: Hidden because superseded. `include_deprecated=True` un-hides THESE.
_DEPRECATED = {"H", "W"}
#: Hidden because bitemporally expired — a DIFFERENT limb of the same predicate,
#: and one the flag must NEVER un-hide, because `search_ranked` applies its
#: `invalid_at` clause unconditionally. If these ever became reachable via the
#: flag, graph enrichment would surface what the search beside it cannot.
_EXPIRED = {"Q"}
#: Hidden under the DEFAULT. Not "hidden under every flag value" — that is the
#: distinction the two sets above exist to keep, and conflating them is the
#: defect this suite now pins (review finding on PR #2339).
_HIDDEN = _DEPRECATED | _EXPIRED
#: Far enough in the past to be expired under any clock this test runs on.
_PAST = "2020-01-01T00:00:00+00:00"


def _oracle_visible(node: str, include_deprecated: bool) -> bool:
    """Whether traversal may reach ``node`` at this flag value.

    Expiry is unconditional; deprecation is the caller's choice. Written as the
    two separate limbs rather than as one set difference so that a future change
    to either width has to touch the limb it actually changes.
    """
    if node in _EXPIRED:
        return False
    return include_deprecated or node not in _DEPRECATED


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


async def _ids(store, db, root, *, include_deprecated=False, depth=3):
    """Ids from a store DIRECTLY. Visible mode only — the NetworkX store
    declines `include_deprecated=True` by design, so a hidden traversal is a facade
    question (`_facade_ids`), not a store one."""
    nodes = await store.traverse(
        db, root, max_depth=depth, min_strength=0.0, include_deprecated=include_deprecated
    )
    return [n.memory_id for n in nodes]


async def _cte_ids(db, root, *, include_deprecated=False, depth=3):
    nodes = await _traverse_cte(db, root, depth, 0.0, include_deprecated)
    return [n.memory_id for n in nodes]


async def _facade_ids(db, root, *, include_deprecated=False, depth=3):
    """Ids through the PUBLIC facade — the path a caller actually takes, decline
    and re-route included. This is what must be asserted for the hidden mode."""
    from genesis.memory import graph as graph_mod

    result = await graph_mod.traverse(
        db, root, max_depth=depth, min_strength=0.0, include_deprecated=include_deprecated
    )
    return [n.memory_id for n in result.nodes]


def _oracle_ids(root, *, depth=3, include_deprecated):
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
        if _oracle_visible(src, include_deprecated) and _oracle_visible(tgt, include_deprecated):
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


async def test_include_deprecated_defaults_to_false(vis_db):
    """Passing nothing must behave exactly as passing False — the entire
    existing call graph relies on it (drift.py, dream centrality, the compact
    recall path)."""
    store = NetworkxGraphStore()
    assert await _ids(store, vis_db, "A") == await _ids(
        store, vis_db, "A", include_deprecated=False
    )
    assert await _cte_ids(vis_db, "A") == await _cte_ids(vis_db, "A", include_deprecated=False)


# ── the fix ───────────────────────────────────────────────────────────────


async def test_hidden_root_returns_its_neighbours_when_asked(vis_db):
    """THE defect (#1896): the caller asked for the deprecated memory, so its
    neighbours must come back too."""
    assert await _facade_ids(vis_db, "H", include_deprecated=True) == ["E"]
    assert await _cte_ids(vis_db, "H", include_deprecated=True) == ["E"]
    # Independent expectation, not a second reading of the same engine.
    assert _oracle_ids("H", include_deprecated=True) == ["E"]


async def test_hidden_neighbour_appears_when_asked(vis_db):
    """Distinct from the root question, and the issue says so explicitly: this
    is about a hidden neighbour of a VISIBLE root."""
    assert "H" in await _facade_ids(vis_db, "A", include_deprecated=True)
    assert "H" in await _cte_ids(vis_db, "A", include_deprecated=True)
    assert "H" in _oracle_ids("A", include_deprecated=True)


async def test_traversal_passes_through_a_hidden_node_when_asked(vis_db):
    """The pass-through leg of the equivalence argument, in the other
    direction: with the flag on, E is reachable at depth 2 via H."""
    from genesis.memory import graph as graph_mod

    result = await graph_mod.traverse(
        vis_db, "A", max_depth=2, min_strength=0.0, include_deprecated=True
    )
    by_id = {n.memory_id: n for n in result.nodes}
    assert "E" in by_id and by_id["E"].depth == 2, "E at depth 2 requires crossing H"
    cte = await _traverse_cte(vis_db, "A", 2, 0.0, True)
    assert {n.memory_id for n in cte} >= {"H", "E"}
    assert "E" in _oracle_ids("A", depth=2, include_deprecated=True)


async def test_flag_on_never_loses_a_node_the_default_showed(vis_db):
    """Un-hiding is strictly additive. A fix that reached hidden nodes by
    RESTRUCTURING the walk could satisfy every test above and still drop a
    visible neighbour."""
    store = NetworkxGraphStore()
    for root in ("A", "C", "D", "X", "W", "Q"):
        off = set(await _ids(store, vis_db, root))
        on = set(await _facade_ids(vis_db, root, include_deprecated=True))
        assert off <= on, f"{root}: {off - on} disappeared when un-hiding"


# ── cross-backend parity: which backend answers must not matter ───────────


@pytest.mark.parametrize("include_deprecated", [False, True])
@pytest.mark.parametrize("root", ["A", "C", "D", "E", "H", "X", "W", "Q"])
async def test_backends_agree_field_for_field(vis_db, root, include_deprecated):
    cte = await _traverse_cte(vis_db, root, 3, 0.0, include_deprecated)
    cte_ids = [n.memory_id for n in cte]

    if not include_deprecated:
        # Two real engines answering the same question, field for field.
        store = NetworkxGraphStore()
        walk = await store.traverse(
            vis_db, root, max_depth=3, min_strength=0.0, include_deprecated=False
        )

        def shape(ns):
            return [(n.memory_id, n.link_type, n.depth, n.strength) for n in ns]

        assert shape(walk) == shape(cte)
    else:
        # The NetworkX store declines this mode, so there is no second engine to
        # compare against — the ORACLE stands in, which is stronger than
        # comparing the facade (which re-routes here) to the CTE it routes to.
        assert cte_ids == _oracle_ids(root, include_deprecated=True), (
            "the CTE's hidden-mode answer diverges from the walk's semantics"
        )
        assert await _facade_ids(vis_db, root, include_deprecated=True) == cte_ids, (
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
    assert "include_deprecated" not in sig.parameters


# ── both limbs of the predicate, not just the deprecated one ──────────────


@pytest.mark.parametrize("backend", ["nx", "cte"])
async def test_an_EXPIRED_memory_stays_hidden_even_when_deprecated_is_asked_for(vis_db, backend):
    """The flag un-hides ONE of the predicate's two limbs, and this pins which.

    The predicate hides for two independent reasons: a non-zero `deprecated` and
    an `invalid_at` in the past. Q is expired and NOT deprecated, so it reaches
    the second limb only — and no value of `include_deprecated` may reach it.

    This test asserted the OPPOSITE until a review finding on PR #2339. The flag
    used to be called `include_hidden` and suppressed the whole predicate, so
    `memory_recall(include_deprecated=True)` returned expired graph neighbours
    that its own search could never return: `db.crud.memory.search_ranked`
    applies its `invalid_at` clause unconditionally and gates only the
    `deprecated` one. Graph enrichment must not surface what the search beside it
    hides, which is the rule the wider flag broke.

    Nothing exercised this limb at all before the fixture gained Q: every other
    row is seeded `invalid_at = NULL`, so a change gating only the deprecated
    limb passed the entire suite.
    """

    async def run(root, *, include_deprecated):
        if backend == "nx":
            # Via the facade: the store declines this mode and the facade
            # re-routes it, which is the behaviour a caller actually sees.
            return await _facade_ids(vis_db, root, include_deprecated=include_deprecated)
        return await _cte_ids(vis_db, root, include_deprecated=include_deprecated)

    # Guard-the-guard: Q must really be expired-and-not-deprecated, or this test
    # is just another deprecation test wearing a different name.
    cur = await vis_db.execute(
        "SELECT invalid_at, deprecated FROM memory_metadata WHERE memory_id='Q'"
    )
    invalid_at, deprecated = await cur.fetchone()
    assert invalid_at == _PAST and not deprecated, (
        f"fixture does not isolate the bitemporal limb: {invalid_at=} {deprecated=}"
    )

    assert "Q" not in await run("D", include_deprecated=False), (
        "an expired memory must be hidden by default"
    )
    assert "Q" not in await run("D", include_deprecated=True), (
        "include_deprecated must NOT un-hide an EXPIRED memory — the search beside "
        "this traversal applies its invalid_at filter unconditionally, so a caller "
        "would get a neighbour it could never retrieve as a hit (PR #2339 review)"
    )
    # Unreachable as a ROOT at either flag value, for the same reason.
    assert await run("Q", include_deprecated=False) == []
    assert await run("Q", include_deprecated=True) == [], (
        "an expired memory must not become traversable as a root either"
    )
    # CONTROL, so this cannot pass by the traversal being broken outright: the
    # DEPRECATED limb must still respond to the flag on the very same call.
    assert "W" not in await run("A", include_deprecated=False)
    assert "W" in await run("A", include_deprecated=True), (
        "the deprecated limb stopped responding to the flag — this test would "
        "otherwise pass simply because nothing is ever reachable"
    )


@pytest.mark.parametrize("backend", ["nx", "cte"])
async def test_a_hidden_node_at_DEPTH_2_is_gated(vis_db, backend):
    """W is hidden and sits at depth 2 from A (A->C->W), which is the only way to
    exercise the CTE's RECURSIVE-step visibility clause — its anchor clauses
    decide every depth-1 case. Reverting that clause was observable in 0 of 36
    fixture combinations before W existed.
    """

    async def run(root, *, include_deprecated, depth=3):
        if backend == "nx":
            return await _facade_ids(
                vis_db, root, include_deprecated=include_deprecated, depth=depth
            )
        return await _cte_ids(vis_db, root, include_deprecated=include_deprecated, depth=depth)

    # Guard-the-guard: W must be at depth 2 from A and depth 1 from C.
    assert "W" in await run("C", include_deprecated=True, depth=1), (
        "fixture moved: W is no longer a direct neighbour of C"
    )
    assert "W" not in await run("A", include_deprecated=True, depth=1), (
        "fixture moved: W is reachable from A at depth 1, so this is not a depth-2 case at all"
    )

    assert "W" not in await run("A", include_deprecated=False), (
        "a hidden node at depth 2 leaked with the flag off — the recursive-step "
        "visibility clause is not binding"
    )
    assert "W" in await run("A", include_deprecated=True)


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
        await store.traverse(vis_db, "H", max_depth=2, min_strength=0.0, include_deprecated=True)
    # Still a GraphUnavailableError, so every existing handler keeps working.
    assert issubclass(GraphModeUnsupported, GraphUnavailableError)


async def test_declining_is_cheap_and_builds_no_projection(vis_db):
    """The decline must happen BEFORE any graph work. A store that built a
    projection and then refused would pay the whole cost for nothing — and the
    withdrawn design's cost is exactly what this route exists to avoid."""
    store = NetworkxGraphStore()
    with pytest.raises(GraphModeUnsupported):
        await store.traverse(vis_db, "H", max_depth=2, min_strength=0.0, include_deprecated=True)
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
        vis_db, "H", max_depth=3, min_strength=0.0, include_deprecated=True
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
            vis_db, "H", max_depth=3, min_strength=0.0, include_deprecated=True
        )
    assert [n.memory_id for n in result.nodes] == ["E"], (
        "guard-the-guard: the call must have actually taken the decline path"
    )
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING], (
        f"declining logged at WARNING: {[r.getMessage() for r in caplog.records]}"
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

    async def traverse(self, db, root_id, *, max_depth, min_strength, include_deprecated=False):
        self.calls.append(include_deprecated)
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
            vis_db, "H", max_depth=3, min_strength=0.0, include_deprecated=True
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
        await graph_mod.traverse(
            vis_db, "H", max_depth=3, min_strength=0.0, include_deprecated=True
        )
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
        await store.traverse(vis_db, "H", max_depth=3, min_strength=0.0, include_deprecated=True)

    with caplog_at(logging.WARNING) as records:
        result = await graph_mod.traverse(
            vis_db, "H", max_depth=3, min_strength=0.0, include_deprecated=True
        )
    assert [n.memory_id for n in result.nodes] == ["E"], (
        "guard-the-guard: the answer must still be correct"
    )
    assert not records, (
        f"declining logged at WARNING on a no-NetworkX install: {[r.getMessage() for r in records]}"
    )
