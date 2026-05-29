"""Dream cycle phase: orphan detection.

Finds memories with zero inbound AND zero outbound links in the
knowledge graph. For each orphan, attempts to discover connections
via embedding similarity to linked memories. Creates ``related_to``
links for discoverable connections; flags unreachable orphans as
observations for review.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import aiosqlite
    from qdrant_client import QdrantClient

    from genesis.memory.store import MemoryStore
    from genesis.routing.router import Router

logger = logging.getLogger(__name__)

MAX_ORPHANS_PER_RUN: int = 200
SIMILARITY_THRESHOLD: float = 0.80
COLLECTION: str = "episodic_memory"


async def run_orphan_detection(
    *,
    qdrant: QdrantClient,
    db: aiosqlite.Connection,
    router: Router,
    store: MemoryStore,
    run_id: str,
    dry_run: bool,
) -> dict[str, Any]:
    """Find zero-link memories and attempt to connect them."""
    report: dict[str, Any] = {
        "total_memories": 0,
        "linked_memories": 0,
        "orphans_found": 0,
        "orphans_connected": 0,
        "orphans_flagged": 0,
    }

    # 1. All non-deprecated memory_ids
    cursor = await db.execute(
        "SELECT memory_id FROM memory_metadata WHERE deprecated = 0"
    )
    all_ids = {row[0] for row in await cursor.fetchall()}
    report["total_memories"] = len(all_ids)

    if not all_ids:
        return report

    # 2. All memory_ids that appear in links (either direction)
    cursor = await db.execute(
        "SELECT DISTINCT source_id FROM memory_links "
        "UNION "
        "SELECT DISTINCT target_id FROM memory_links"
    )
    linked_ids = {row[0] for row in await cursor.fetchall()}
    report["linked_memories"] = len(linked_ids & all_ids)

    # 3. Orphans = non-deprecated memories with zero links
    orphans = all_ids - linked_ids
    report["orphans_found"] = len(orphans)

    if not orphans:
        logger.info("Orphan detection: no orphans found among %d memories", len(all_ids))
        return report

    logger.info(
        "Orphan detection: %d orphans out of %d non-deprecated memories (%.1f%%)",
        len(orphans), len(all_ids), 100 * len(orphans) / len(all_ids),
    )

    if dry_run:
        report["sample_orphans"] = list(orphans)[:20]
        return report

    # 4. For each orphan (capped), find similar linked memories
    from genesis.db.crud import memory_links
    from genesis.qdrant import collections as qdrant_ops

    orphan_list = list(orphans)[:MAX_ORPHANS_PER_RUN]
    vectors = qdrant_ops.batch_retrieve_vectors(
        qdrant, orphan_list, collection=COLLECTION,
    )

    connected = 0
    flagged = 0

    for oid in orphan_list:
        vec = vectors.get(oid)
        if vec is None:
            continue

        # Search for similar memories (excluding deprecated)
        hits = qdrant_ops.search(
            qdrant,
            collection=COLLECTION,
            query_vector=vec,
            limit=6,
        )

        # Filter: only linked memories, exclude self
        neighbors = [
            h for h in hits
            if h["id"] != oid
            and h["id"] in linked_ids
            and h["score"] >= SIMILARITY_THRESHOLD
        ]

        if neighbors:
            # Create related_to links to top 3 neighbors
            now_iso = datetime.now(UTC).isoformat()
            for neighbor in neighbors[:3]:
                try:
                    await memory_links.create(
                        db,
                        source_id=oid,
                        target_id=neighbor["id"],
                        link_type="related_to",
                        strength=round(neighbor["score"], 4),
                        created_at=now_iso,
                    )
                except Exception:  # noqa: PERF203
                    logger.debug("Link %s→%s already exists", oid[:8], neighbor["id"][:8])
            connected += 1
        else:
            flagged += 1

    report["orphans_connected"] = connected
    report["orphans_flagged"] = flagged

    # Invalidate graph cache if links created
    if connected > 0:
        try:
            from genesis.memory.graph import invalidate_graph_cache

            invalidate_graph_cache()
        except ImportError:
            pass

    logger.info(
        "Orphan detection: connected %d, flagged %d (of %d processed)",
        connected, flagged, len(orphan_list),
    )
    return report
