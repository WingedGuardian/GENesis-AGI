"""Knowledge graph traversal using recursive CTEs.

Provides graph queries over the memory_links table for exploring
connected memories, traversing relationship chains, and finding
clusters of related knowledge.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import aiosqlite

logger = logging.getLogger(__name__)


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


async def traverse(
    db: aiosqlite.Connection,
    root_id: str,
    *,
    max_depth: int = 3,
    min_strength: float = 0.0,
) -> TraversalResult:
    """Traverse the memory graph from a root node using recursive CTE.

    Follows outgoing links (source_id → target_id) up to max_depth hops.
    Filters by minimum link strength. Tracks visited nodes to prevent cycles.

    Args:
        db: Database connection.
        root_id: Starting memory ID.
        max_depth: Maximum traversal depth (default 3).
        min_strength: Minimum link strength to follow (default 0.0).

    Returns:
        TraversalResult with connected nodes and query timing.
    """
    start = time.monotonic()

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
    elapsed_ms = (time.monotonic() - start) * 1000

    nodes = [
        GraphNode(
            memory_id=row[0],
            link_type=row[1],
            depth=row[2],
            strength=row[3],
        )
        for row in rows
    ]

    if elapsed_ms > 100:
        logger.warning(
            "Graph traversal from %s took %.1fms (threshold: 100ms, "
            "%d nodes, depth %d)",
            root_id, elapsed_ms, len(nodes), max_depth,
        )

    return TraversalResult(root_id=root_id, nodes=nodes, query_ms=elapsed_ms)


async def find_connected_by_type(
    db: aiosqlite.Connection,
    root_id: str,
    link_type: str,
    *,
    max_depth: int = 2,
) -> list[GraphNode]:
    """Find memories connected by a specific link type.

    Useful for queries like "what was evaluated for X?" or
    "what decisions relate to X?".
    """
    start = time.monotonic()

    cursor = await db.execute(
        """
        WITH RECURSIVE connected(target_id, link_type, depth, strength, path) AS (
            SELECT target_id, link_type, 1, strength,
                   source_id || ',' || target_id
            FROM memory_links
            WHERE source_id = ?
              AND link_type = ?
            UNION ALL
            SELECT ml.target_id, ml.link_type, c.depth + 1, ml.strength,
                   c.path || ',' || ml.target_id
            FROM memory_links ml
            JOIN connected c ON ml.source_id = c.target_id
            WHERE c.depth < ?
              AND ml.link_type = ?
              AND c.path NOT LIKE '%' || ml.target_id || '%'
        )
        SELECT DISTINCT target_id, link_type, depth, strength
        FROM connected
        ORDER BY depth, strength DESC
        """,
        (root_id, link_type, max_depth, link_type),
    )

    rows = await cursor.fetchall()
    elapsed_ms = (time.monotonic() - start) * 1000

    if elapsed_ms > 100:
        logger.warning(
            "Typed traversal (%s) from %s took %.1fms",
            link_type, root_id, elapsed_ms,
        )

    return [
        GraphNode(
            memory_id=row[0],
            link_type=row[1],
            depth=row[2],
            strength=row[3],
        )
        for row in rows
    ]


async def get_cluster(
    db: aiosqlite.Connection,
    root_id: str,
    *,
    max_depth: int = 2,
    min_strength: float = 0.5,
) -> list[str]:
    """Get all memory IDs in a cluster around root_id.

    Follows links in BOTH directions (source→target and target→source)
    to find the full connected component.
    """
    start = time.monotonic()

    cursor = await db.execute(
        """
        WITH RECURSIVE cluster(mem_id, depth, path) AS (
            SELECT target_id, 1, ? || ',' || target_id
            FROM memory_links
            WHERE source_id = ? AND strength >= ?
            UNION ALL
            SELECT source_id, 1, ? || ',' || source_id
            FROM memory_links
            WHERE target_id = ? AND strength >= ?
            UNION ALL
            SELECT
                CASE WHEN ml.source_id = c.mem_id THEN ml.target_id
                     ELSE ml.source_id END,
                c.depth + 1,
                c.path || ',' ||
                CASE WHEN ml.source_id = c.mem_id THEN ml.target_id
                     ELSE ml.source_id END
            FROM memory_links ml
            JOIN cluster c ON (ml.source_id = c.mem_id OR ml.target_id = c.mem_id)
            WHERE c.depth < ?
              AND ml.strength >= ?
              AND c.path NOT LIKE '%' ||
                  CASE WHEN ml.source_id = c.mem_id THEN ml.target_id
                       ELSE ml.source_id END || '%'
        )
        SELECT DISTINCT mem_id FROM cluster
        """,
        (root_id, root_id, min_strength,
         root_id, root_id, min_strength,
         max_depth, min_strength),
    )

    rows = await cursor.fetchall()
    elapsed_ms = (time.monotonic() - start) * 1000

    if elapsed_ms > 100:
        logger.warning(
            "Cluster query from %s took %.1fms (%d members)",
            root_id, elapsed_ms, len(rows),
        )

    return [row[0] for row in rows]
