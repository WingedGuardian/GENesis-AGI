"""Memory-graph facade — picks a backend, owns the public read surface.

The traversal and centrality logic now live behind the ``GraphStore`` seam
(``memory/graphstore.py``); this module is what the rest of Genesis imports. It
owns the single production store instance, because ``invalidate_graph_cache()``
is called by every ``memory_links`` writer — 13 call sites across 9 modules at
the time of writing — none of which holds a store reference, and several of
which hold no database handle either.

Fallback: the recursive CTE at the bottom of this module, reached on ONE
condition — the active store raised ``GraphUnavailableError``. A cold cache is
NOT a trigger, though this docstring said so for years: the store's first query
builds its projection and returns it. Worth stating precisely, because the wrong
version made the fallback sound routine when it is in fact dormant on a healthy
install — which is how the two paths were free to disagree unnoticed.

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
from datetime import UTC, datetime
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
        try:
            nodes = await _traverse_cte(db, root_id, max_depth, min_strength)
        except Exception as cte_exc:
            # The fallback reads the SAME connection the store just failed on,
            # so every non-transient cause — a closed handle, a missing table, a
            # corrupt file — fails it identically. Without this, making the
            # store raise properly only moved the leak one layer: the store's
            # error was caught here and the CTE's raw one escaped in its place.
            # MEASURED against this facade on a closed connection: `traverse()`
            # raised a bare `ValueError: no active connection` at the caller,
            # after logging a line that said it was falling back.
            #
            # The one cause the fallback genuinely rescues is a transient
            # `database is locked`, which is why it still runs first.
            raise GraphUnavailableError(
                f"the graph store and its SQL fallback both failed: {cte_exc}"
            ) from cte_exc

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
    still returns [] — zero nodes means zero bridges). The NetworkX store
    additionally raises when the library itself is unimportable. Deliberately
    does NOT fall back: a decision-tier consumer must never be handed a
    silently different metric.

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
    """Original recursive CTE traversal (fallback).

    The walk this mirrors is `graphstore_nx._bfs_with_strength` — it lives
    behind the seam now, not above this function, and these two implementations
    answering differently is exactly what must not happen.

    ONE ROW PER MEMORY, picked the same way the NetworkX walk picks: shallowest
    depth, then the strongest edge reaching it, then link_type as a deterministic
    tie-break. That is not tidiness — it is the same correctness property this
    module's walk exists to provide, and the fallback used to contradict it.

    `SELECT DISTINCT target_id, link_type, depth, strength` keeps one row per
    COMBINATION, not per memory, so a node reached through two parents at the
    same depth came back TWICE — once credited its strongest edge and once its
    weakest. `mcp/memory/core.py` takes `traversal.nodes[:5]` and does NOT sort
    it — the order this function emits IS the selection — so the duplicate both
    occupied two of those five slots and dragged a false weaker strength into
    what the model reads. Which implementation answers must not change that.

    The window's ORDER BY mirrors that walk's `(strength, link_type)` maximum
    exactly, and the outer `ORDER BY depth, strength DESC, target_id` mirrors the
    walk's committed sequence — `(-strength, memory_id)` within a level,
    preserved through a stable final sort on `(depth, -strength)`. That matters
    because `drift.py:202` reads this sequence as a RANKED list for RRF without
    reading a single label, so two implementations agreeing on every field and
    disagreeing on order still hand that consumer different answers.

    The trailing `target_id` is EXPLICIT, not load-bearing, and the distinction is
    measured rather than assumed: with three equal-strength neighbours inserted in
    a deliberately adversarial order (z, a, m), this query returns them id-sorted
    WITH the key and identically WITHOUT it — the window's `PARTITION BY
    target_id` already groups them that way. So no test can tell the two apart,
    and none claims to. It is kept because that is a property of one engine's
    query plan, which SQLite does not promise, and stating the order costs
    nothing; do not read it as a guard something exercises.

    Window functions need SQLite >= 3.25 (2018); this install runs 3.45, and the
    repo already hard-depends on 3.35+ elsewhere (`UPDATE…RETURNING`,
    `ALTER TABLE DROP COLUMN` in migrations 0010/0014/0016), so this floor sits
    strictly below an existing one and cannot newly break a clone.

    Carries the SAME visibility predicate as the graph stores — a degraded path
    that showed the model memories the primary path hides would be worse than
    the degradation itself. Expressed in SQL here (rather than reusing
    ``invalid_memory_ids``) because the traversal is recursive; the cost is
    bounded by the edges actually walked, not the whole table.

    GUARDS ON THE ANCHOR, because the anchor row skipped constraints both the
    recursive step and the walk apply — one generator, several symptoms. Two of
    them are described here; the visibility clauses above are the others:

    * `target_id <> source_id`. The walk seeds `visited = {root_id}` and can
      therefore never emit the root; the anchor had no such guard, so a memory
      linked to itself was returned as its own related memory, burning one of the
      five slots `core.py` shows. MEASURED on the live table: 30 of 269,757 rows
      are self-links, so this fired for 30 roots.
    * `max_depth < 1` returns early (below). The anchor emits depth 1
      unconditionally, ignoring the bound that walk's `while depth < max_depth`
      respects — so `max_depth=0` asked for nothing and got a level. Inert today
      (no caller passes 0; production passes 1, 2 or the default 3) and closed
      anyway, because the claim being made here is that the two paths agree.
    """
    if max_depth < 1:
        return []
    now = datetime.now(UTC).isoformat()
    cursor = await db.execute(
        """
        WITH RECURSIVE connected(target_id, link_type, depth, strength, path) AS (
            SELECT target_id, link_type, 1, strength,
                   source_id || ',' || target_id
            FROM memory_links
            WHERE source_id = ?
              AND target_id <> source_id
              AND strength >= ?
              -- The ROOT is filtered too. The NX loader drops an edge when
              -- EITHER endpoint is hidden, so a hidden memory has no edges at
              -- all there; filtering only the target here would let the CTE
              -- traverse FROM a hidden root and return a subtree the primary
              -- path returns nothing for. MEASURED: 2,827 live memories are
              -- hidden AND have out-edges, and the two forms otherwise
              -- classify 6,503 edges (2.5% of the graph) differently.
              AND NOT EXISTS (SELECT 1 FROM memory_metadata m
                              WHERE m.memory_id = memory_links.source_id
                                AND ((m.invalid_at IS NOT NULL AND m.invalid_at <= ?)
                                  OR m.deprecated != 0))
              AND NOT EXISTS (SELECT 1 FROM memory_metadata m
                              WHERE m.memory_id = memory_links.target_id
                                AND ((m.invalid_at IS NOT NULL AND m.invalid_at <= ?)
                                  OR m.deprecated != 0))
            UNION ALL
            SELECT ml.target_id, ml.link_type, c.depth + 1, ml.strength,
                   c.path || ',' || ml.target_id
            FROM memory_links ml
            JOIN connected c ON ml.source_id = c.target_id
            WHERE c.depth < ?
              AND ml.strength >= ?
              AND c.path NOT LIKE '%' || ml.target_id || '%'
              AND NOT EXISTS (SELECT 1 FROM memory_metadata m
                              WHERE m.memory_id = ml.target_id
                                AND ((m.invalid_at IS NOT NULL AND m.invalid_at <= ?)
                                  OR m.deprecated != 0))
        )
        SELECT target_id, link_type, depth, strength
        FROM (
            SELECT target_id, link_type, depth, strength,
                   ROW_NUMBER() OVER (
                       PARTITION BY target_id
                       ORDER BY depth ASC, strength DESC, link_type DESC
                   ) AS rn
            FROM connected
        )
        WHERE rn = 1
        ORDER BY depth, strength DESC, target_id
        """,
        (root_id, min_strength, now, now, max_depth, min_strength, now),
    )
    rows = await cursor.fetchall()
    return [
        GraphNode(memory_id=row[0], link_type=row[1], depth=row[2], strength=row[3])
        for row in rows
    ]
