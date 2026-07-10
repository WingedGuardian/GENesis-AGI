"""Knowledge graph traversal with NetworkX caching.

Primary path: in-memory NetworkX DiGraph loaded lazily from memory_links.
Fallback: recursive CTE queries (if NetworkX import fails or cache is cold
during the first query of a session).

The cache is invalidated via ``invalidate_graph_cache()`` when links are
created or deleted. The next query triggers a rebuild from SQLite.
"""

from __future__ import annotations

import logging
import time
from collections import deque
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

_nx_graph: object | None = None  # nx.DiGraph when _NX_AVAILABLE
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

    G = nx.DiGraph()
    for source_id, target_id, link_type, strength in rows:
        G.add_edge(
            source_id, target_id,
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
    G: object,  # nx.DiGraph
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
    queue: deque[tuple[str, int]] = deque([(root_id, 0)])
    results: list[GraphNode] = []

    while queue:
        node, depth = queue.popleft()
        if depth >= max_depth:
            continue

        for _, neighbor, data in G.out_edges(node, data=True):
            if neighbor in visited:
                continue
            strength = data.get("strength", 0.0)
            edge_type = data.get("link_type", "")

            if strength < min_strength:
                continue
            if link_type_filter and edge_type != link_type_filter:
                continue

            visited.add(neighbor)
            results.append(GraphNode(
                memory_id=neighbor,
                link_type=edge_type,
                depth=depth + 1,
                strength=strength,
            ))
            queue.append((neighbor, depth + 1))

    # Match CTE output order: depth ascending, strength descending
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
    top_n: int = 100,
) -> list[tuple[str, float]]:
    """Return top-N memories by betweenness centrality.

    Identifies memories that are "bridges" between clusters of knowledge.
    Requires NetworkX; returns empty list if unavailable.
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
    return ranked[:top_n]


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
