"""Dream cycle phase: link repair.

Finds and removes orphaned links where source_id or target_id references
a memory that no longer exists in memory_metadata. Pure SQL — no Qdrant
or LLM calls.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import aiosqlite
    from qdrant_client import QdrantClient

    from genesis.memory.store import MemoryStore
    from genesis.routing.router import Router

logger = logging.getLogger(__name__)


async def run_link_repair(
    *,
    qdrant: QdrantClient,
    db: aiosqlite.Connection,
    router: Router,
    store: MemoryStore,
    run_id: str,
    dry_run: bool,
) -> dict[str, Any]:
    """Find and remove orphaned links from memory_links.

    An orphaned link references a memory_id (as source or target) that
    does not exist in memory_metadata. These accumulate when memories are
    hard-deleted (rare) or when rollback/cleanup operations miss link
    cleanup.
    """
    report: dict[str, Any] = {
        "links_checked": 0,
        "orphaned_removed": 0,
        "orphaned_ids": [],
    }

    # 1. Collect all memory_ids referenced in links
    cursor = await db.execute(
        "SELECT DISTINCT source_id FROM memory_links "
        "UNION "
        "SELECT DISTINCT target_id FROM memory_links"
    )
    link_memory_ids = {row[0] for row in await cursor.fetchall()}
    report["links_checked"] = len(link_memory_ids)

    if not link_memory_ids:
        return report

    # 2. Collect all existing memory_ids from metadata
    cursor = await db.execute("SELECT memory_id FROM memory_metadata")
    existing_ids = {row[0] for row in await cursor.fetchall()}

    # 3. Find orphans: referenced in links but not in metadata
    orphaned = link_memory_ids - existing_ids
    if not orphaned:
        logger.info("Link repair: no orphaned references found in %d IDs", len(link_memory_ids))
        return report

    report["orphaned_ids"] = list(orphaned)[:100]  # cap for report readability
    logger.info(
        "Link repair: found %d orphaned memory references out of %d total",
        len(orphaned), len(link_memory_ids),
    )

    if dry_run:
        report["orphaned_removed"] = 0
        report["would_remove"] = len(orphaned)
        return report

    # 4. Delete all links involving orphaned IDs
    total_deleted = 0
    for oid in orphaned:
        cursor = await db.execute(
            "DELETE FROM memory_links WHERE source_id = ? OR target_id = ?",
            (oid, oid),
        )
        total_deleted += cursor.rowcount

    await db.commit()
    report["orphaned_removed"] = total_deleted

    # 5. Invalidate graph cache
    if total_deleted > 0:
        try:
            from genesis.memory.graph import invalidate_graph_cache

            invalidate_graph_cache()
        except ImportError:
            pass

    logger.info(
        "Link repair: removed %d links involving %d orphaned IDs",
        total_deleted, len(orphaned),
    )
    return report
