"""Knowledge graph traversal with NetworkX caching.

Primary path: in-memory NetworkX MultiDiGraph loaded lazily from memory_links
(a multigraph because one memory pair may carry several typed edges).
Fallback: recursive CTE queries (if NetworkX import fails or cache is cold
during the first query of a session).

The cache is invalidated via ``invalidate_graph_cache()`` when links are
created or deleted. The next query triggers a rebuild from SQLite.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import aiosqlite

logger = logging.getLogger(__name__)

try:
    import networkx as nx

    _NX_AVAILABLE = True
except ImportError:  # pragma: no cover
    _NX_AVAILABLE = False

# ─── Data structures ──────────────────────────────────────────────────────────

@dataclass
class GraphNode:
    """A node in a traversal result."""

    memory_id: str
    link_type: str
    depth: int
    strength: float


@dataclass
class TraversalResult:
    """Result of a graph traversal query."""

    root_id: str
    nodes: list[GraphNode]
    query_ms: float


# ─── NetworkX cache ───────────────────────────────────────────────────────────

_nx_graph: object | None = None  # nx.MultiDiGraph when _NX_AVAILABLE
_nx_dirty: bool = True


def invalidate_graph_cache() -> None:
    """Mark the in-memory graph as stale.

    Called by the linker after link creation/deletion. The next query
    triggers a full rebuild from memory_links.
    """
    global _nx_dirty
    _nx_dirty = True


async def _ensure_graph(db: aiosqlite.Connection) -> object:
    """Lazy-load the graph from memory_links, rebuild if dirty."""
    global _nx_graph, _nx_dirty

    if _nx_graph is not None and not _nx_dirty:
        return _nx_graph

    start = time.monotonic()
    cursor = await db.execute(
        "SELECT source_id, target_id, link_type, strength FROM memory_links"
    )
    rows = await cursor.fetchall()

    # MultiDiGraph, not DiGraph: memory_links' primary key is
    # (source_id, target_id, link_type), so one pair may legitimately carry
    # several typed edges. A DiGraph cannot hold parallel edges — the second
    # add_edge for a pair overwrites the first's attributes — so the graph kept
    # an arbitrary survivor and the strength/link_type filters below were
    # evaluated against it.
    G = nx.MultiDiGraph()
    for source_id, target_id, link_type, strength in rows:
        G.add_edge(
            source_id, target_id,
            key=link_type,
            link_type=link_type, strength=strength,
        )

    elapsed_ms = (time.monotonic() - start) * 1000
    logger.info(
        "Graph cache rebuilt: %d nodes, %d edges in %.1fms",
        G.number_of_nodes(), G.number_of_edges(), elapsed_ms,
    )
    if G.number_of_edges() > 50_000:
        logger.warning(
            "Graph has %d edges — measure NetworkX rebuild cost; consider an "
            "incremental or server-backed graph if rebuilds become a bottleneck",
            G.number_of_edges(),
        )

    _nx_graph = G
    _nx_dirty = False
    return G


def _bfs_with_strength(
    G: object,  # nx.MultiDiGraph
    root_id: str,
    *,
    max_depth: int,
    min_strength: float,
    link_type_filter: str | None = None,
) -> list[GraphNode]:
    """BFS traversal with edge-attribute filtering.

    NetworkX's bfs_edges doesn't filter by edge attributes, so we roll
    a simple BFS that respects min_strength and optional link_type.
    """
    if root_id not in G:
        return []

    visited: set[str] = {root_id}
    frontier: list[str] = [root_id]
    results: list[GraphNode] = []
    depth = 0

    while frontier and depth < max_depth:

        # A pair may carry several typed edges (MultiDiGraph), so out_edges
        # yields one tuple per parallel edge. Consider them all and keep the
        # STRONGEST that passes the filters — otherwise the arbitrary survivor
        # merely moves from load time to traversal time (the first parallel
        # edge would win via the `visited` check) and a weak edge could still
        # mask a strong one.
        #
        # Strength-max mirrors memory_links.neighbors_of's MAX(strength)
        # collapse, but note neighbors_of returns no link_type — this path is
        # the only one that must also PICK a type, so strongest-wins is a
        # deliberate choice, not an inherited convention. Revisit if polarity
        # types start appearing on multi-type pairs (0 of 139 today carry
        # `contradicts` as their max-strength edge). Note the consumers of THIS
        # path (mcp/memory/core.py:431,:704) put link_type in front of the model
        # and do NOT exclude `contradicts` — graph_expansion, which does exclude
        # it, reaches the graph through memory_links_crud.neighbors_of and never
        # calls this function. So demoting the type here is a small real
        # improvement on the one path that surfaces it, not consistency with a
        # subsystem that already filters it elsewhere.
        #
        # The comparison is on the (strength, link_type) TUPLE, not on strength
        # alone, because strength alone leaves ties to row order: 106 of the 139
        # live multi-type pairs carry EQUAL strengths (MEASURED 2026-09-02), and
        # the loader's SELECT has no ORDER BY, so a strength-only max would keep
        # reporting an arbitrary type for those — the same defect as the DiGraph
        # collapse, just narrowed. The secondary key is a DETERMINISTIC tie-break,
        # not a semantic ranking; it happens to demote `contradicts` (which sorts
        # early), the safe direction. 0 of the 106 tied pairs carries one.
        #
        # `best` is scoped to the whole LEVEL, not to one expanding parent, and
        # that is the point. Per-parent, a node reachable from several parents was
        # claimed by whichever one the queue happened to reach first — so the
        # reported edge followed the loader's row order (its SELECT has no ORDER
        # BY) and could be the WEAKER of the two. That is not cosmetic: `strength`
        # is put in front of the model AND is what the consumers sort on before
        # taking the top five, so a node credited to a weaker parent sinks in that
        # order and can leave the slice entirely.
        #
        # MEASURED against this module on the live graph, old vs new, over the FULL
        # population (256,063 links; all 66,856 roots that have neighbours; the real
        # call parameters max_depth=2, min_strength=0.3):
        #   68,330 of 1,066,912 reported nodes (6.40%) gained a higher, truer
        #     strength, across 50.4% of roots; 0 were ever lowered.
        #   top-five SET churn between a forward and a reversed row order: 3.09%
        #     before, 0 after; the full output is likewise identical under both.
        #   reach-set unchanged (0 roots) and no reported depth changed (0 nodes).
        # Draining the level is what allows the cross-parent comparison.
        #
        # State the DENOMINATOR when quoting any of this. 6.40% is over every node
        # the walk computes (~16 per root); restricted to the five that actually
        # reach the model it is 0.16% of surfaced nodes and 0.7% of lookups. The
        # blast radius at that slice is separate again: the surfaced SET changes on
        # 1.94% of roots and its ORDER on 8.15%. An earlier revision of this comment
        # quoted a 1,000-root sample and read an order of magnitude high on the
        # surfaced surface — which is why every figure here is a population count.
        #
        # Sample-drawn figures also drift between runs on an unchanged table: the
        # loader's SELECT has no ORDER BY, so node insertion order — and any sample
        # drawn from it — varies per rebuild. Prefer the population numbers above.
        #
        # The commit below is ordered by a TOTAL key, and that alone is what makes
        # the whole output deterministic: it fixes the append sequence, and the
        # final sort is stable, so equal `(depth, -strength)` keys keep that
        # sequence rather than the row order they used to keep. Note `drift.py`
        # consumes that sequence as a RANKED list for RRF (its `local_ids`,
        # drift.py:202) even though it reads no labels, so the ordering here is
        # load-bearing for a second consumer, not just for the sliced view.
        #
        # A total key on the FINAL sort as well was tried and dropped. It is
        # redundant by construction — a stable sort of an already-deterministic
        # list cannot reintroduce nondeterminism — and measured redundant too
        # (0 differences either way across 1,447 live roots). Keeping it would only
        # have reordered ties gratuitously and widened the divergence from the CTE
        # fallback's documented `(depth, strength DESC)`.
        best: dict[str, tuple[float, str]] = {}
        for node in frontier:
            for _, neighbor, data in G.out_edges(node, data=True):
                if neighbor in visited:
                    continue
                strength = data.get("strength", 0.0)
                edge_type = data.get("link_type", "")

                if strength < min_strength:
                    continue
                if link_type_filter and edge_type != link_type_filter:
                    continue

                current = best.get(neighbor)
                if current is None or (strength, edge_type) > current:
                    best[neighbor] = (strength, edge_type)

        next_frontier: list[str] = []
        # Key is (-strength, neighbour_id) and DELIBERATELY excludes link_type,
        # unlike the within-pair comparison above. The id alone already makes the
        # key total, so link_type buys no determinism here — and it is not neutral:
        # sorting equal-strength neighbours alphabetically by TYPE front-loads
        # early-sorting relationships in the order the model reads. MEASURED over
        # all 66,856 roots, 13.8% of which carry a (depth, strength) tie group:
        # including link_type moved the reported top-1 type by +32.8%
        # (categorized_as), +31.8% (action_item_for), -23.8% (preceded_by) and
        # -39.6% (succeeded_by) against the id-only key. Memory ids are UUIDs, so
        # they carry no such correlation. The within-pair key above is a different
        # case: there the two candidates are the SAME pair and a type must be
        # picked, so a stated rule beats an arbitrary one.
        for neighbor, (strength, edge_type) in sorted(
            best.items(), key=lambda kv: (-kv[1][0], kv[0])
        ):
            visited.add(neighbor)
            results.append(GraphNode(
                memory_id=neighbor,
                link_type=edge_type,
                depth=depth + 1,
                strength=strength,
            ))
            next_frontier.append(neighbor)
        frontier = next_frontier
        depth += 1

    # Match CTE output order: depth ascending, strength descending. Deliberately
    # left as a partial key — ties now resolve to the deterministic commit order
    # established above, so no further tiebreak is needed to make this stable.
    results.sort(key=lambda n: (n.depth, -n.strength))
    return results


# ─── Public API ───────────────────────────────────────────────────────────────


async def traverse(
    db: aiosqlite.Connection,
    root_id: str,
    *,
    max_depth: int = 3,
    min_strength: float = 0.0,
) -> TraversalResult:
    """Traverse the memory graph from a root node.

    Uses NetworkX cache when available, falls back to recursive CTE.

    Args:
        db: Database connection.
        root_id: Starting memory ID.
        max_depth: Maximum traversal depth (default 3).
        min_strength: Minimum link strength to follow (default 0.0).

    Returns:
        TraversalResult with connected nodes and query timing.
    """
    start = time.monotonic()

    if _NX_AVAILABLE:
        G = await _ensure_graph(db)
        nodes = _bfs_with_strength(
            G, root_id, max_depth=max_depth, min_strength=min_strength,
        )
    else:
        nodes = await _traverse_cte(db, root_id, max_depth, min_strength)

    elapsed_ms = (time.monotonic() - start) * 1000

    if elapsed_ms > 100:
        logger.warning(
            "Graph traversal from %s took %.1fms (threshold: 100ms, "
            "%d nodes, depth %d)",
            root_id, elapsed_ms, len(nodes), max_depth,
        )

    return TraversalResult(root_id=root_id, nodes=nodes, query_ms=elapsed_ms)


# ─── New NetworkX-only functions ──────────────────────────────────────────────


async def centrality_scores(
    db: aiosqlite.Connection,
    top_n: int | None = 100,
) -> list[tuple[str, float]]:
    """Return memories ranked by betweenness centrality.

    Identifies memories that are "bridges" between clusters of knowledge.
    Requires NetworkX; returns empty list if unavailable.

    ``top_n`` caps the returned slice; ``top_n=None`` returns EVERY scored
    node (the full ranking). Betweenness is computed over all nodes regardless
    — ``top_n`` is only a post-sort slice — so ``None`` adds no compute cost,
    just a longer list.
    """
    if not _NX_AVAILABLE:
        return []

    G = await _ensure_graph(db)
    if G.number_of_nodes() == 0:
        return []

    # Use approximate betweenness for large graphs to avoid blocking
    n_nodes = G.number_of_nodes()
    k = min(200, n_nodes) if n_nodes > 200 else None
    scores = nx.betweenness_centrality(G, k=k)
    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    return ranked if top_n is None else ranked[:top_n]


# ─── CTE fallbacks ───────────────────────────────────────────────────────────


async def _traverse_cte(
    db: aiosqlite.Connection,
    root_id: str,
    max_depth: int,
    min_strength: float,
) -> list[GraphNode]:
    """Original recursive CTE traversal (fallback)."""
    cursor = await db.execute(
        """
        WITH RECURSIVE connected(target_id, link_type, depth, strength, path) AS (
            SELECT target_id, link_type, 1, strength,
                   source_id || ',' || target_id
            FROM memory_links
            WHERE source_id = ?
              AND strength >= ?
            UNION ALL
            SELECT ml.target_id, ml.link_type, c.depth + 1, ml.strength,
                   c.path || ',' || ml.target_id
            FROM memory_links ml
            JOIN connected c ON ml.source_id = c.target_id
            WHERE c.depth < ?
              AND ml.strength >= ?
              AND c.path NOT LIKE '%' || ml.target_id || '%'
        )
        SELECT DISTINCT target_id, link_type, depth, strength
        FROM connected
        ORDER BY depth, strength DESC
        """,
        (root_id, min_strength, max_depth, min_strength),
    )
    rows = await cursor.fetchall()
    return [
        GraphNode(memory_id=row[0], link_type=row[1], depth=row[2], strength=row[3])
        for row in rows
    ]
