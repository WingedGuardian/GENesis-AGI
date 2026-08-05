"""Dream cycle phase: link repair.

Two passes, both pure SQL (no Qdrant/LLM):

1. **Orphan removal** — links whose ``source_id``/``target_id`` reference a
   memory absent from ``memory_metadata`` (hard-deleted, or missed by a
   rollback/cleanup).
2. **Aged deprecated-edge prune** — edges of dream-merged originals whose
   synthesis was created at least ``deprecated_edge_prune_days`` ago. The merge
   COPIES an original's external edges onto the synthesis and soft-deletes the
   original (keeping metadata/links for rollback), so the original's own edges
   linger, dangling on a deprecated node. This ages them out — EXCEPT the
   ``extends`` provenance links, which are the rollback trail. The window must
   exceed the rollback review window (rollback cannot restore pruned edges).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
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
    now: str | None = None,
) -> dict[str, Any]:
    """Repair the ``memory_links`` graph: orphan removal + aged-deprecated prune.

    ``now`` (ISO) is injectable for deterministic tests; production uses wall
    clock. Both passes honour ``dry_run``.
    """
    report: dict[str, Any] = {
        "links_checked": 0,
        "orphaned_removed": 0,
        "orphaned_ids": [],
        "deprecated_edges_removed": 0,
        "would_remove_deprecated": 0,
        "aged_deprecated_ids": 0,
    }
    graph_dirty = False

    # ── Pass 1: orphan removal ────────────────────────────────────────────────
    cursor = await db.execute(
        "SELECT DISTINCT source_id FROM memory_links "
        "UNION "
        "SELECT DISTINCT target_id FROM memory_links"
    )
    link_memory_ids = {row[0] for row in await cursor.fetchall()}
    report["links_checked"] = len(link_memory_ids)

    if link_memory_ids:
        cursor = await db.execute("SELECT memory_id FROM memory_metadata")
        existing_ids = {row[0] for row in await cursor.fetchall()}
        orphaned = link_memory_ids - existing_ids
        if orphaned:
            report["orphaned_ids"] = list(orphaned)[:100]  # cap for readability
            logger.info(
                "Link repair: found %d orphaned memory references out of %d total",
                len(orphaned), len(link_memory_ids),
            )
            if dry_run:
                report["would_remove"] = len(orphaned)
            else:
                total_deleted = 0
                for oid in orphaned:
                    cursor = await db.execute(
                        "DELETE FROM memory_links WHERE source_id = ? OR target_id = ?",
                        (oid, oid),
                    )
                    total_deleted += cursor.rowcount
                await db.commit()
                report["orphaned_removed"] = total_deleted
                graph_dirty = graph_dirty or total_deleted > 0
                logger.info(
                    "Link repair: removed %d links involving %d orphaned IDs",
                    total_deleted, len(orphaned),
                )

    # ── Pass 2: aged deprecated-edge prune ────────────────────────────────────
    graph_dirty = await _prune_aged_deprecated_edges(db, report, dry_run=dry_run, now=now) or graph_dirty

    if graph_dirty:
        try:
            from genesis.memory.graph import invalidate_graph_cache

            invalidate_graph_cache()
        except ImportError:
            pass

    return report


async def _prune_aged_deprecated_edges(
    db: aiosqlite.Connection,
    report: dict[str, Any],
    *,
    dry_run: bool,
    now: str | None,
) -> bool:
    """Prune non-``extends`` edges of dream-deprecated originals aged past the
    configured window. Returns True if any edge was deleted (graph dirtied).

    Age is derived from the synthesis stamp — dream deprecation carries no
    ``deprecated_at`` column, but the synthesis of run ``R`` is stamped
    ``dream_cycle_run_id = 'synthesis:R'`` while its originals carry ``R``; the
    synthesis row's ``created_at`` is the deprecation time. Originals with no
    ``dream_cycle_run_id`` (e.g. entity-adjudication deprecations) are never
    touched here — nothing prunes those today.
    """
    from genesis.memory import dream_shield_config

    prune_days = dream_shield_config.knob_int(
        dream_shield_config.load_config(), "deprecated_edge_prune_days"
    )
    now_dt = datetime.fromisoformat(now) if now else datetime.now(UTC)
    cutoff = (now_dt - timedelta(days=prune_days)).isoformat()

    rows = await db.execute_fetchall(
        "SELECT dep.memory_id FROM memory_metadata dep "
        "JOIN memory_metadata syn "
        "  ON syn.dream_cycle_run_id = 'synthesis:' || dep.dream_cycle_run_id "
        "WHERE dep.deprecated = 1 AND dep.dream_cycle_run_id IS NOT NULL "
        "GROUP BY dep.memory_id "
        "HAVING MIN(syn.created_at) < ?",
        (cutoff,),
    )
    aged_ids = [r[0] for r in rows]
    report["aged_deprecated_ids"] = len(aged_ids)
    if not aged_ids:
        return False

    if dry_run:
        # Count the non-extends edges that WOULD be removed.
        would = 0
        for mid in aged_ids:
            rows = await db.execute_fetchall(
                "SELECT COUNT(*) FROM memory_links "
                "WHERE (source_id = ? OR target_id = ?) AND link_type != 'extends'",
                (mid, mid),
            )
            would += rows[0][0]
        report["would_remove_deprecated"] = would
        return False

    removed = 0
    for mid in aged_ids:
        cursor = await db.execute(
            "DELETE FROM memory_links "
            "WHERE (source_id = ? OR target_id = ?) AND link_type != 'extends'",
            (mid, mid),
        )
        removed += cursor.rowcount
    await db.commit()
    report["deprecated_edges_removed"] = removed
    logger.info(
        "Link repair: pruned %d aged deprecated edges across %d originals "
        "(> %d days)", removed, len(aged_ids), prune_days,
    )
    return removed > 0
