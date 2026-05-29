"""Dream cycle phase: entity resolution scan.

Finds near-duplicate memories via Qdrant similarity search and applies
confidence-tiered resolution:

- **≥0.95 cosine**: auto-merge (newer survives, older deprecated)
- **0.85–0.95 cosine**: LLM semantic check (duplicate / contradicts / distinct)
- **<0.85**: skip

Every action is logged to ``entity_resolution_audit`` for post-hoc review.
Contradictions create ``contradicts`` or ``succeeded_by`` links in the graph.
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

COLLECTION: str = "episodic_memory"


async def run_entity_resolution(
    *,
    qdrant: QdrantClient,
    db: aiosqlite.Connection,
    router: Router,
    store: MemoryStore,
    run_id: str,
    dry_run: bool,
) -> dict[str, Any]:
    """Dedup + contradiction detection across episodic memories."""
    from genesis.memory.entity_resolution import (
        AUTO_MERGE_THRESHOLD,
        LLM_CHECK_FLOOR,
        MAX_ENTITY_CHECKS_PER_RUN,
        check_semantic_overlap,
        find_dedup_candidates,
        log_resolution,
    )
    from genesis.qdrant import collections as qdrant_ops

    report: dict[str, Any] = {
        "candidates_found": 0,
        "auto_merged": 0,
        "llm_checked": 0,
        "llm_merged": 0,
        "contradictions": 0,
        "skipped": 0,
        "errors": [],
    }

    # 1. Scroll non-deprecated episodic memories, grouped by (wing, room)
    from genesis.memory.dream_cycle import _scroll_and_group

    buckets = await _scroll_and_group(qdrant)
    total_points = sum(len(pts) for pts in buckets.values())
    report["total_points"] = total_points

    if total_points < 2:
        return report

    llm_checks_used = 0

    for (_wing, _room), points in buckets.items():
        if len(points) < 2:
            continue

        # 2. Fetch vectors for this bucket
        point_ids = [p["id"] for p in points]
        vectors = qdrant_ops.batch_retrieve_vectors(
            qdrant, point_ids, collection=COLLECTION,
        )

        # 3. Find dedup candidates (cosine ≥ 0.92)
        candidates = await find_dedup_candidates(
            qdrant, points, vectors, collection=COLLECTION,
        )
        report["candidates_found"] += len(candidates)

        for point_a, point_b, score in candidates:
            payload_a = point_a.get("payload", {})
            payload_b = point_b.get("payload", {})
            content_a = payload_a.get("content", "")
            content_b = payload_b.get("content", "")

            if dry_run:
                report.setdefault("sample_candidates", [])
                if len(report["sample_candidates"]) < 10:
                    report["sample_candidates"].append({
                        "score": round(score, 4),
                        "id_a": point_a["id"][:12],
                        "id_b": point_b["id"][:12],
                        "preview_a": content_a[:80],
                        "preview_b": content_b[:80],
                    })
                continue

            # Determine which memory is newer (survivor)
            ts_a = payload_a.get("created_at", "")
            ts_b = payload_b.get("created_at", "")
            if ts_a >= ts_b:
                survivor_id, deprecated_id = point_a["id"], point_b["id"]
            else:
                survivor_id, deprecated_id = point_b["id"], point_a["id"]

            if score >= AUTO_MERGE_THRESHOLD:
                # Auto-merge: deprecate older, keep newer
                try:
                    await _deprecate_memory(
                        qdrant, db, deprecated_id,
                        survivor_id=survivor_id,
                        run_id=run_id,
                    )
                    await log_resolution(
                        db,
                        run_id=run_id,
                        action="auto_merge",
                        memory_id_a=point_a["id"],
                        memory_id_b=point_b["id"],
                        content_a=content_a,
                        content_b=content_b,
                        cosine_score=score,
                        survivor_id=survivor_id,
                    )
                    report["auto_merged"] += 1
                except Exception as exc:
                    report["errors"].append({
                        "action": "auto_merge",
                        "ids": [point_a["id"][:12], point_b["id"][:12]],
                        "error": str(exc),
                    })
                    logger.warning(
                        "Entity auto-merge failed: %s", exc, exc_info=True,
                    )

            elif score >= LLM_CHECK_FLOOR and llm_checks_used < MAX_ENTITY_CHECKS_PER_RUN:
                # LLM semantic check
                llm_checks_used += 1
                report["llm_checked"] += 1

                verdict = await check_semantic_overlap(
                    router, content_a, content_b,
                )
                rel = verdict.get("relationship", "distinct")
                reasoning = verdict.get("reasoning", "")

                if rel == "duplicate":
                    try:
                        await _deprecate_memory(
                            qdrant, db, deprecated_id,
                            survivor_id=survivor_id,
                            run_id=run_id,
                        )
                        await log_resolution(
                            db,
                            run_id=run_id,
                            action="llm_merge",
                            memory_id_a=point_a["id"],
                            memory_id_b=point_b["id"],
                            content_a=content_a,
                            content_b=content_b,
                            cosine_score=score,
                            llm_verdict=rel,
                            llm_reasoning=reasoning,
                            survivor_id=survivor_id,
                        )
                        report["llm_merged"] += 1
                    except Exception as exc:
                        report["errors"].append({
                            "action": "llm_merge",
                            "error": str(exc),
                        })

                elif rel == "contradicts":
                    # Create contradiction or succession link
                    link_type = "contradicts"
                    # Temporal contradiction: newer supersedes older
                    if ts_a != ts_b:
                        link_type = "succeeded_by"

                    try:
                        from genesis.db.crud import memory_links

                        # Older → newer for succeeded_by; either direction for contradicts
                        if link_type == "succeeded_by":
                            src, tgt = deprecated_id, survivor_id
                        else:
                            src, tgt = point_a["id"], point_b["id"]

                        await memory_links.create(
                            db,
                            source_id=src,
                            target_id=tgt,
                            link_type=link_type,
                            strength=round(score, 4),
                            created_at=datetime.now(UTC).isoformat(),
                        )
                    except Exception:
                        pass  # PK collision = link already exists

                    await log_resolution(
                        db,
                        run_id=run_id,
                        action="contradiction" if link_type == "contradicts" else "succeeded_by",
                        memory_id_a=point_a["id"],
                        memory_id_b=point_b["id"],
                        content_a=content_a,
                        content_b=content_b,
                        cosine_score=score,
                        llm_verdict=rel,
                        llm_reasoning=reasoning,
                    )
                    report["contradictions"] += 1

                else:
                    # distinct — log and skip
                    await log_resolution(
                        db,
                        run_id=run_id,
                        action="skipped",
                        memory_id_a=point_a["id"],
                        memory_id_b=point_b["id"],
                        cosine_score=score,
                        llm_verdict=rel,
                        llm_reasoning=reasoning,
                    )
                    report["skipped"] += 1

    # Invalidate graph cache if any merges or links created
    if report["auto_merged"] + report["llm_merged"] + report["contradictions"] > 0:
        try:
            from genesis.memory.graph import invalidate_graph_cache

            invalidate_graph_cache()
        except ImportError:
            pass

    logger.info(
        "Entity resolution: %d candidates, %d auto-merged, %d LLM-checked "
        "(%d merged, %d contradictions, %d distinct), %d errors",
        report["candidates_found"],
        report["auto_merged"],
        report["llm_checked"],
        report["llm_merged"],
        report["contradictions"],
        report["skipped"],
        len(report["errors"]),
    )
    return report


async def _deprecate_memory(
    qdrant: QdrantClient,
    db: aiosqlite.Connection,
    memory_id: str,
    *,
    survivor_id: str,
    run_id: str,
) -> None:
    """Two-layer deprecation: Qdrant payload + SQLite metadata.

    Mirrors the deprecation logic in ``dream_cycle._synthesize_and_deprecate``
    but without creating a new synthesized memory.
    """
    from genesis.qdrant.collections import update_payload

    # Qdrant: mark as deprecated
    update_payload(
        qdrant,
        collection=COLLECTION,
        point_id=memory_id,
        payload={
            "deprecated": True,
            "merged_into": survivor_id,
        },
    )

    # SQLite: mark as deprecated
    await db.execute(
        "UPDATE memory_metadata SET deprecated = 1, "
        "dream_cycle_run_id = ? WHERE memory_id = ?",
        (run_id, memory_id),
    )
    await db.commit()
