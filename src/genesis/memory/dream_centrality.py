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
        "nodes_persisted": 0,
        "top_score": 0.0,
        "computation_ms": 0.0,
    }

    from genesis.memory.graph import GraphUnavailableError, centrality_scores

    t0 = time.monotonic()
    try:
        # top_n=None: persist the FULL scored ranking (not just the top-500).
        # Betweenness already computes every node — the widening only changes
        # how many rows land in the cache, which the importance shield reads
        # as its bridge-node population.
        scores = await centrality_scores(db, top_n=None)
    except GraphUnavailableError as exc:
        # The store could not answer — which is NOT "no bridges". Returning
        # here (before any DELETE below) keeps the previous cache standing, so
        # the importance shield keeps its last real threshold instead of
        # silently shielding nothing. The stale-cache cost is bounded: the
        # next successful run atomically replaces it.
        logger.warning("Centrality skipped — graph unavailable: %s", exc)
        report["graph_unavailable"] = True
        report["error"] = str(exc)
        return report
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

    # Persist only NONZERO scores. Betweenness is heavily zero-inflated (with
    # k-pivot approximation most nodes score exactly 0); persisting zeros would
    # make a percentile over the cache degenerate to ~0 and shield everything,
    # and would bloat the table toward the full collection size.
    nonzero = [(mid, score) for mid, score in scores if score > 0.0]
    report["nodes_persisted"] = len(nonzero)

    if not nonzero:
        # Still clear the cache — a run that finds no bridges supersedes stale
        # rows (atomic replace, same as the populated path).
        await db.execute("DELETE FROM centrality_cache")
        await db.commit()
        logger.info(
            "Centrality: %d nodes scored, 0 nonzero (empty/degenerate graph?)",
            len(scores),
        )
        return report

    # Atomic replacement: delete all + insert batch
    now_iso = datetime.now(UTC).isoformat()
    await db.execute("DELETE FROM centrality_cache")
    await db.executemany(
        "INSERT INTO centrality_cache (memory_id, centrality_score, computed_at) "
        "VALUES (?, ?, ?)",
        [(mid, round(score, 8), now_iso) for mid, score in nonzero],
    )
    await db.commit()

    logger.info(
        "Centrality: cached %d/%d nonzero scores in %.1fms (top: %.6f)",
        len(nonzero), len(scores), elapsed_ms, nonzero[0][1],
    )
    return report
