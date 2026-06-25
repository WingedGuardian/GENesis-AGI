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

import json
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
DEDUP_CHUNK_SIZE: int = 200


def _parse_ts(ts: str) -> datetime:
    """Parse ISO timestamp, returning datetime.min on failure."""
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except (ValueError, TypeError):
        return datetime.min.replace(tzinfo=UTC)


async def run_entity_resolution(
    *,
    qdrant: QdrantClient,
    db: aiosqlite.Connection,
    router: Router,
    store: MemoryStore,
    run_id: str,
    dry_run: bool,
    buckets: dict | None = None,
) -> dict[str, Any]:
    """Dedup + contradiction detection across episodic memories.

    Args:
        buckets: Pre-scrolled (wing, room) → points dict from consolidation.
            If None, scrolls Qdrant independently (slower, used in standalone).
    """
    from genesis.memory.entity_resolution import (
        AUTO_MERGE_THRESHOLD,
        EVIDENCE_THRESHOLD,
        LLM_CHECK_FLOOR,
        MAX_ENTITY_CHECKS_PER_RUN,
        check_semantic_overlap,
        compute_evidence_strength,
        find_dedup_candidates,
        log_resolution,
        pick_duplicate_survivor,
    )
    from genesis.qdrant import collections as qdrant_ops

    report: dict[str, Any] = {
        "candidates_found": 0,
        "auto_merged": 0,
        "low_evidence_skipped": 0,
        "llm_checked": 0,
        "llm_merged": 0,
        "contradictions": 0,
        "skipped": 0,
        "errors": [],
    }

    # 1. Use pre-scrolled buckets if available, otherwise scroll independently
    if buckets is None:
        from genesis.memory.dream_cycle import _scroll_and_group

        buckets = await _scroll_and_group(qdrant)
    total_points = sum(len(pts) for pts in buckets.values())
    report["total_points"] = total_points

    if total_points < 2:
        return report

    # Process each bucket in chunks to bound memory.  For buckets within
    # DEDUP_CHUNK_SIZE, single-pass (no overhead).  For larger buckets,
    # split into chunks and process each independently; cross-chunk pairs
    # are handled via a shared processed_pairs set and global Qdrant search.
    llm_checks_used = 0

    for (_wing, _room), points in buckets.items():
        if len(points) < 2:
            continue

        # Split into chunks (single-element list for small buckets).
        if len(points) <= DEDUP_CHUNK_SIZE:
            chunks = [points]
        else:
            chunks = [
                points[i : i + DEDUP_CHUNK_SIZE]
                for i in range(0, len(points), DEDUP_CHUNK_SIZE)
            ]
            logger.info(
                "Entity resolution: bucket (%s, %s) has %d points — "
                "processing in %d chunks of ≤%d",
                _wing, _room, len(points), len(chunks), DEDUP_CHUNK_SIZE,
            )

        # Track pairs already processed across chunks (Qdrant searches
        # globally, so chunk N may re-find a pair from chunk M).
        processed_pairs: set[tuple[str, str]] = set()
        # Track IDs deprecated by earlier chunks in this bucket so we
        # don't re-process them as search sources (in-memory payload
        # is stale; Qdrant is updated but not re-scrolled).
        deprecated_this_run: set[str] = set()

        for _chunk_idx, chunk in enumerate(chunks):
            # Filter out points deprecated by earlier chunks.
            active_chunk = [
                p for p in chunk if p["id"] not in deprecated_this_run
            ]
            if len(active_chunk) < 2:
                continue

            # 2. Fetch vectors for this chunk
            point_ids = [p["id"] for p in active_chunk]
            vectors = qdrant_ops.batch_retrieve_vectors(
                qdrant, point_ids, collection=COLLECTION,
            )

            # 3. Find dedup candidates (cosine ≥ 0.92)
            candidates = await find_dedup_candidates(
                qdrant, active_chunk, vectors, collection=COLLECTION,
            )

            new_candidates = 0
            for point_a, point_b, score in candidates:
                # Skip pairs already processed by earlier chunks.
                pair_key = tuple(sorted((point_a["id"], point_b["id"])))
                if pair_key in processed_pairs:
                    continue
                processed_pairs.add(pair_key)

                # Skip if either point was deprecated by an earlier chunk.
                # Qdrant global search still returns deprecated points (no
                # payload filter); without this guard we'd re-deprecate and
                # double-count.
                if point_a["id"] in deprecated_this_run or point_b["id"] in deprecated_this_run:
                    continue

                new_candidates += 1

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
                dt_a = _parse_ts(ts_a)
                dt_b = _parse_ts(ts_b)
                if dt_a >= dt_b:
                    survivor_id, deprecated_id = point_a["id"], point_b["id"]
                else:
                    survivor_id, deprecated_id = point_b["id"], point_a["id"]

                if score >= AUTO_MERGE_THRESHOLD:
                    # Evidence gate (spec ③): high cosine alone is not enough.
                    # A near-floor merge whose corroborating signals (temporal
                    # distance, confidence, load-bearing) are jointly weak is
                    # flagged for review instead of being silently deprecated.
                    strength, evidence = compute_evidence_strength(
                        payload_a, payload_b, score,
                    )
                    if strength < EVIDENCE_THRESHOLD:
                        await log_resolution(
                            db,
                            run_id=run_id,
                            action="flagged",
                            memory_id_a=point_a["id"],
                            memory_id_b=point_b["id"],
                            content_a=content_a,
                            content_b=content_b,
                            cosine_score=score,
                            llm_verdict="low_evidence",
                            llm_reasoning=json.dumps({
                                "reason": "low_evidence_auto_merge",
                                "strength": round(strength, 4),
                                "threshold": EVIDENCE_THRESHOLD,
                                "signals": evidence,
                            }),
                        )
                        report["low_evidence_skipped"] += 1
                        continue

                    # Confirmed duplicate: keep the load-bearing memory as
                    # survivor (not merely the newer one — survivor fix).
                    survivor_id, deprecated_id = pick_duplicate_survivor(
                        point_a["id"], payload_a, dt_a,
                        point_b["id"], payload_b, dt_b,
                    )
                    # Auto-merge: deprecate the non-survivor, keep the survivor
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
                        deprecated_this_run.add(deprecated_id)
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
                        # Adversarial second opinion: different-provider
                        # LLM challenges the "duplicate" verdict before
                        # permanent deprecation. Both must agree.
                        try:
                            from genesis.memory.adversarial_review import (
                                check_entity_duplicate,
                            )
                            second_opinion = await check_entity_duplicate(
                                router=router,
                                content_a=content_a,
                                content_b=content_b,
                            )
                            if second_opinion["relationship"] == "distinct":
                                logger.info(
                                    "Entity challenge override: %s vs %s — "
                                    "primary=duplicate, challenge=distinct (%s). "
                                    "Keeping both.",
                                    point_a["id"][:8], point_b["id"][:8],
                                    second_opinion["reasoning"],
                                )
                                report.setdefault("challenge_overrides", 0)
                                report["challenge_overrides"] += 1
                                await log_resolution(
                                    db,
                                    run_id=run_id,
                                    action="flagged",
                                    memory_id_a=point_a["id"],
                                    memory_id_b=point_b["id"],
                                    content_a=content_a,
                                    content_b=content_b,
                                    cosine_score=score,
                                    llm_verdict="challenge_override_to_distinct",
                                    llm_reasoning=second_opinion["reasoning"],
                                )
                                continue  # Skip deprecation
                        except Exception:
                            # Challenge call failed — fail-safe: skip deprecation
                            logger.debug(
                                "Entity challenge call failed, preserving both",
                                exc_info=True,
                            )
                            continue

                        # Confirmed duplicate: keep the load-bearing memory as
                        # survivor (consistent with the auto-merge path).
                        survivor_id, deprecated_id = pick_duplicate_survivor(
                            point_a["id"], payload_a, dt_a,
                            point_b["id"], payload_b, dt_b,
                        )
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
                            deprecated_this_run.add(deprecated_id)
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

                        # Resolve link direction BEFORE the try so the error
                        # handler below can never hit an unbound name if the
                        # import or create() raises.
                        # Older → newer for succeeded_by; either direction for contradicts
                        if link_type == "succeeded_by":
                            src, tgt = deprecated_id, survivor_id
                        else:
                            src, tgt = point_a["id"], point_b["id"]

                        try:
                            from genesis.db.crud import memory_links

                            await memory_links.create(
                                db,
                                source_id=src,
                                target_id=tgt,
                                link_type=link_type,
                                strength=round(score, 4),
                                created_at=datetime.now(UTC).isoformat(),
                            )
                        except Exception as link_exc:
                            # Only PK collision is expected; log unexpected errors
                            if "UNIQUE constraint" not in str(link_exc):
                                logger.warning(
                                    "Contradiction link %s→%s failed: %s",
                                    src[:8], tgt[:8], link_exc,
                                )

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

            report["candidates_found"] += new_candidates

    # Invalidate graph cache if any merges or links created
    if report["auto_merged"] + report["llm_merged"] + report["contradictions"] > 0:
        try:
            from genesis.memory.graph import invalidate_graph_cache

            invalidate_graph_cache()
        except ImportError:
            pass

    logger.info(
        "Entity resolution: %d candidates, %d auto-merged, "
        "%d low-evidence-flagged, %d LLM-checked "
        "(%d merged, %d contradictions, %d distinct), %d errors",
        report["candidates_found"],
        report["auto_merged"],
        report["low_evidence_skipped"],
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
