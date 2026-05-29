"""Dream cycle phase: centrality recomputation.

Computes betweenness centrality scores using the existing
``graph.centrality_scores()`` function and persists them in the
``centrality_cache`` table. Runs even in dry_run mode since the
cache is read-only data with no destructive effects.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import aiosqlite
    from qdrant_client import QdrantClient

    from genesis.memory.store import MemoryStore
    from genesis.routing.router import Router

logger = logging.getLogger(__name__)

TOP_N: int = 500


async def run_centrality_recompute(
    *,
    qdrant: QdrantClient,
    db: aiosqlite.Connection,
    router: Router,
    store: MemoryStore,
    run_id: str,
    dry_run: bool,
) -> dict[str, Any]:
    """Compute and cache betweenness centrality scores.

    Runs the existing ``centrality_scores()`` from ``graph.py`` (which
    uses k-approximation for large graphs) and replaces the
    ``centrality_cache`` table atomically.

    Runs even in dry_run — centrality is read-only observational data,
    not a destructive operation.
    """
    report: dict[str, Any] = {
        "nodes_scored": 0,
        "top_score": 0.0,
        "computation_ms": 0.0,
    }

    from genesis.memory.graph import centrality_scores

    t0 = time.monotonic()
    try:
        scores = await centrality_scores(db, top_n=TOP_N)
    except Exception as exc:
        logger.warning("Centrality computation failed: %s", exc, exc_info=True)
        report["error"] = str(exc)
        return report

    elapsed_ms = (time.monotonic() - t0) * 1000
    report["computation_ms"] = round(elapsed_ms, 1)
    report["nodes_scored"] = len(scores)

    if scores:
        report["top_score"] = round(scores[0][1], 6)
        report["top_memory"] = scores[0][0]

    if not scores:
        logger.info("Centrality: no scores computed (empty graph?)")
        return report

    # Atomic replacement: delete all + insert batch
    now_iso = datetime.now(UTC).isoformat()
    await db.execute("DELETE FROM centrality_cache")
    await db.executemany(
        "INSERT INTO centrality_cache (memory_id, centrality_score, computed_at) "
        "VALUES (?, ?, ?)",
        [(mid, round(score, 8), now_iso) for mid, score in scores],
    )
    await db.commit()

    logger.info(
        "Centrality: cached %d scores in %.1fms (top: %.6f)",
        len(scores), elapsed_ms, scores[0][1],
    )
    return report
