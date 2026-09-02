"""Parallel typed edges must survive the graph load.

`memory_links`' production primary key is ``(source_id, target_id, link_type)``
(migration 0029: "distinct relationship types between the same pair … must
coexist, not silently overwrite"), so ONE memory pair can legitimately carry
several typed edges.

`graph.py` loaded those rows into an ``nx.DiGraph``, which cannot hold parallel
edges: the second ``add_edge`` for a pair OVERWRITES the first one's attributes.
The graph therefore kept an ARBITRARY survivor per pair, and the survivor is
what ``_bfs_with_strength``'s ``min_strength`` / ``link_type_filter`` checks
were evaluated against — so a weak or wrong-typed survivor could hide a
neighbour that a strong, matching parallel edge really does reach.

MEASURED on the live database (2026-09-02): 252,525 link rows collapsed to
252,383 distinct pairs — 142 edges lost across 139 multi-type pairs — and 0 of
those pairs straddled a live ``min_strength`` threshold. So no recall result is
known to be wrong today; these tests lock the correctness property before an
engine migration builds on the loader, and they fail on the pre-fix code.

Note the fixture below uses the PRODUCTION primary key. The pre-existing
fixture in ``test_graph.py`` declares ``PRIMARY KEY (source_id, target_id)``,
which structurally forbids the shape under test — which is precisely why the
existing suite could never have caught this.
"""

from __future__ import annotations

import aiosqlite
import pytest

from genesis.memory.graph import invalidate_graph_cache, traverse

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def parallel_db():
    """A DB whose memory_links PK matches production (pair + link_type)."""
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await db.execute(
        """
        CREATE TABLE memory_links (
            source_id   TEXT NOT NULL,
            target_id   TEXT NOT NULL,
            link_type   TEXT NOT NULL,
            strength    REAL NOT NULL DEFAULT 0.5,
            created_at  TEXT NOT NULL,
            PRIMARY KEY (source_id, target_id, link_type)
        )
        """
    )
    # Under a DiGraph the surviving edge is whichever row loads LAST, so which
    # edge survives depends on the SELECT's row order — and the loader's query
    # carries no ORDER BY. Insertion order is NOT enough to pin that: an index
    # scan would resolve EVERY pair by the same alphabetical link_type rule, so
    # "two pairs inserted in opposite orders" still leaves both keeping the same
    # side. The link_types below are chosen so the WEAK edge sorts LAST for A→B
    # and FIRST for A→D:
    #   rowid / PK-index ASC  -> A→B keeps the weak 'supports' 0.2
    #   reversed / PK DESC    -> A→D keeps the weak 'action_item_for' 0.2
    # so at least one pair is broken under every plausible scan order, and the
    # assertions below (which require BOTH pairs correct) fail on the old
    # loader in all of them. VERIFIED across all four orders, not assumed.
    # (Two earlier revisions of this fixture were vacuous — first by insertion
    # order, then by index order — which is why the property is now pinned in
    # the DATA rather than in a comment.)
    links = [
        ("A", "B", "extends", 0.9),
        ("A", "B", "supports", 0.2),
        ("A", "D", "action_item_for", 0.2),
        ("A", "D", "extends", 0.9),
        ("A", "C", "extends", 0.8),
    ]
    for src, tgt, link_type, strength in links:
        await db.execute(
            "INSERT INTO memory_links VALUES (?, ?, ?, ?, '2026-09-02')",
            (src, tgt, link_type, strength),
        )
    await db.commit()

    invalidate_graph_cache()
    yield db
    invalidate_graph_cache()
    await db.close()


async def test_parallel_edges_both_survive_the_load(parallel_db):
    """Both typed A→B rows must exist in the loaded graph, not just one."""
    from genesis.memory.graph import _ensure_graph

    graph = await _ensure_graph(parallel_db)
    # 5 rows in, 5 edges out. A DiGraph collapses each multi-type pair to one
    # edge and yields 3.
    assert graph.number_of_edges() == 5


async def test_weak_parallel_edge_cannot_mask_a_strong_one(parallel_db):
    """min_strength must consider EVERY parallel edge, not an arbitrary one.

    A→B and A→D each carry a 0.2 and a 0.9 edge, so at min_strength=0.5 both
    are reachable via their 0.9 'extends' edge. If the loader kept only a weak
    survivor for a pair, that neighbour is wrongly filtered out entirely.
    """
    result = await traverse(parallel_db, "A", max_depth=1, min_strength=0.5)
    ids = {n.memory_id for n in result.nodes}
    assert {"B", "D"} <= ids, "a strong parallel edge was masked by a weak one"
    assert "C" in ids


async def test_reported_edge_is_the_strongest_passing_one(parallel_db):
    """The surfaced link_type/strength must not be an arbitrary parallel edge.

    Results are ordered by strength and sliced to the top 5 before reaching the
    model (mcp/memory/core.py), so reporting 0.2/'supports' when a 0.9/'extends'
    edge exists is wrong on both counts — and can reorder that slice.
    """
    result = await traverse(parallel_db, "A", max_depth=1, min_strength=0.0)
    by_id = {n.memory_id: n for n in result.nodes}
    for pair in ("B", "D"):
        assert by_id[pair].strength == pytest.approx(0.9), pair
        assert by_id[pair].link_type == "extends", pair
