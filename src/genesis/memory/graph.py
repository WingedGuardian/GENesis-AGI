"""Memory-graph facade — picks a backend, owns the public read surface.

The traversal and centrality logic now live behind the ``GraphStore`` seam
(``memory/graphstore.py``); this module is what the rest of Genesis imports. It
owns the single production store instance, because ``invalidate_graph_cache()``
is called by every ``memory_links`` writer — 13 call sites across 9 modules at
the time of writing — none of which holds a store reference, and several of
which hold no database handle either.

Backend today: ``NetworkxGraphStore`` — the in-process MultiDiGraph projection,
unchanged. When NetworkX cannot be imported at all, ``traverse`` still degrades
to the recursive-CTE fallback exactly as before; ``centrality_scores``
deliberately does NOT degrade — it raises, because its consumer (the importance
shield) treats "unavailable" and "empty" oppositely.

The seam exists for the graph-DB adoption (issue #1641): a server-backed engine
becomes another ``GraphStore`` and this facade's selection changes, with no
reader touched. The four readers are ``mcp/memory/core.py`` (recall enrichment
and ``memory_expand``), ``memory/drift.py``, and ``memory/dream_centrality.py``.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from genesis.memory.graphstore import (
    GraphNode,
    GraphStore,
    GraphUnavailableError,
    TraversalResult,
)

# Re-exported deliberately: `_bfs_with_strength` is imported ACROSS packages by
# eval/graph_bakeoff/engines/nx_incremental.py, which reuses production's exact
# BFS so the bake-off control is honest. Moving it must not break that import.
from genesis.memory.graphstore_nx import (  # noqa: F401
    NetworkxGraphStore,
    _bfs_with_strength,
)

if TYPE_CHECKING:  # pragma: no cover
    import aiosqlite

logger = logging.getLogger(__name__)

__all__ = [
    "GraphNode",
    "GraphUnavailableError",
    "TraversalResult",
    "centrality_scores",
    "invalidate_graph_cache",
    "traverse",
]

# The ONE production store. Module-level by necessity, not convenience: the
# writer sites that invalidate it (memory_links CRUD, linker, the dream jobs,
# connection pass, integrity repair) reach it through a lazy `from ... import
# invalidate_graph_cache` and have no other handle on it.
#
# Annotated against the protocol deliberately: CI runs no type checker, so this
# annotation plus the conformance test is the only thing standing between a
# future backend and silently violating the raise-never-return-empty contract.
_store: GraphStore = NetworkxGraphStore()


def _reset_store_for_tests() -> None:
    """Drop the production store and its pinned connection.

    Mirrors ``memory/health.py::_reset_top_tags_state``. The store holds a
    strong reference to the last connection it built from, which over a long
    test session keeps closed aiosqlite connections (each a Thread) alive.
    """
    global _store
    _store = NetworkxGraphStore()


def invalidate_graph_cache() -> None:
    """Mark the in-memory graph as stale.

    Called by writers after link creation/deletion. The next query triggers a
    full rebuild from memory_links.
    """
    _store.invalidate()


async def traverse(
    db: aiosqlite.Connection,
    root_id: str,
    *,
    max_depth: int = 3,
    min_strength: float = 0.0,
) -> TraversalResult:
    """Traverse the memory graph from a root node.

    Uses the active graph store; falls back to the recursive CTE when the
    store cannot answer at all (today: NetworkX missing).

    Args:
        db: Database connection.
        root_id: Starting memory ID.
        max_depth: Maximum traversal depth (default 3).
        min_strength: Minimum link strength to follow (default 0.0).

    Returns:
        TraversalResult with connected nodes and query timing.
    """
    start = time.monotonic()

    try:
        nodes = await _store.traverse(
            db, root_id, max_depth=max_depth, min_strength=min_strength,
        )
    except GraphUnavailableError as exc:
        # Traversal is an ENRICHMENT path — its readers already treat a thin
        # result as "no neighbours", so degrading to SQL keeps them working.
        # centrality_scores below is the opposite case and must not do this.
        #
        # LOUD, because this stops being a once-per-process import verdict the
        # moment a server-backed store lands: a backend that times out would
        # otherwise route every recall enrichment through SQL while looking
        # perfectly healthy.
        logger.warning(
            "Graph store %r unavailable — falling back to the recursive CTE: %s",
            getattr(_store, "name", "?"), exc, exc_info=True,
        )
        nodes = await _traverse_cte(db, root_id, max_depth, min_strength)

    elapsed_ms = (time.monotonic() - start) * 1000

    if elapsed_ms > 100:
        logger.warning(
            "Graph traversal from %s took %.1fms (threshold: 100ms, "
            "%d nodes, depth %d)",
            root_id, elapsed_ms, len(nodes), max_depth,
        )

    return TraversalResult(root_id=root_id, nodes=nodes, query_ms=elapsed_ms)


async def centrality_scores(
    db: aiosqlite.Connection,
    top_n: int | None = 100,
) -> list[tuple[str, float]]:
    """Return memories ranked by betweenness centrality.

    Identifies memories that are "bridges" between clusters of knowledge.
    Raises GraphUnavailableError if the backend cannot answer (an EMPTY graph
    still returns [] — zero nodes means zero bridges). Deliberately does NOT
    fall back: a decision-tier consumer must never be handed a silently
    different metric.

    ``top_n`` caps the returned slice; ``top_n=None`` returns EVERY scored
    node (the full ranking). Betweenness is computed over all nodes regardless
    — ``top_n`` is only a post-sort slice — so ``None`` adds no compute cost,
    just a longer list.
    """
    return await _store.centrality(db, top_n)


# ─── CTE fallback ────────────────────────────────────────────────────────────


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
