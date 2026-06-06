"""Dream cycle — retroactive episodic memory consolidation.

Sweeps the episodic_memory Qdrant collection on a schedule, identifies
clusters of semantically near-duplicate memories, synthesizes each
cluster into a single canonical memory via LLM, and soft-deletes the
originals.  Analogy: the brain consolidates episodic memories during
sleep.

**Key design decisions:**
- Episodic only — knowledge_base excluded (low volume, upsert handles dedup)
- No time-based confidence decay — evidence-based changes only
- deprecated supplements invalid_at (independent dimensions)
- Dry-run mode is the default until manually reviewed
- Max 100 cluster merges per run (configurable)
- Clusters > 10 memories flagged for manual review, not auto-merged

Spec: docs/superpowers/specs/2026-05-14-dream-cycle-design.md
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import statistics
import uuid
from collections import defaultdict
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import aiosqlite
    from qdrant_client import QdrantClient

    from genesis.memory.store import MemoryStore
    from genesis.routing.router import Router

from genesis.memory.adversarial_review import SynthesisBlockedError

logger = logging.getLogger(__name__)

# ── Configuration ────────────────────────────────────────────────────────

SIMILARITY_THRESHOLD: float = 0.87
MAX_CLUSTER_SIZE: int = 10
MAX_MERGES_PER_RUN: int = 100
MAX_BUCKET_SIZE: int = 500
MIN_AVAILABLE_MB: int = 256
_YIELD_EVERY: int = 50  # yield to event loop every N search calls
CALL_SITE_ID: str = "dream_cycle_synthesis"
COLLECTION: str = "episodic_memory"

# ── Union-Find ───────────────────────────────────────────────────────────


class _UnionFind:
    """Disjoint-set (union-find) with path compression and union by rank."""

    def __init__(self) -> None:
        self._parent: dict[str, str] = {}
        self._rank: dict[str, int] = {}

    def find(self, x: str) -> str:
        if x not in self._parent:
            self._parent[x] = x
            self._rank[x] = 0
        if self._parent[x] != x:
            self._parent[x] = self.find(self._parent[x])
        return self._parent[x]

    def union(self, x: str, y: str) -> None:
        rx, ry = self.find(x), self.find(y)
        if rx == ry:
            return
        if self._rank[rx] < self._rank[ry]:
            rx, ry = ry, rx
        self._parent[ry] = rx
        if self._rank[rx] == self._rank[ry]:
            self._rank[rx] += 1

    def components(self) -> dict[str, list[str]]:
        """Return {root: [members]} for all groups."""
        groups: dict[str, list[str]] = defaultdict(list)
        for x in self._parent:
            groups[self.find(x)].append(x)
        return groups


def _read_mem_available_mb() -> int | None:
    """Read MemAvailable from /proc/meminfo in MB.

    Returns None if unavailable (non-Linux).  Inlined to avoid importing
    the heavy ``genesis.observability.snapshots.infrastructure`` module.
    """
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024  # kB → MB
    except (OSError, ValueError, IndexError):
        pass
    return None


# ── Core ─────────────────────────────────────────────────────────────────


async def run(
    *,
    qdrant: QdrantClient,
    db: aiosqlite.Connection,
    router: Router,
    store: MemoryStore,
    dry_run: bool = True,
    similarity_threshold: float = SIMILARITY_THRESHOLD,
    max_merges: int = MAX_MERGES_PER_RUN,
    max_cluster_size: int = MAX_CLUSTER_SIZE,
) -> dict[str, Any]:
    """Execute one dream cycle run.

    Returns a report dict with cluster counts, merge stats, and any
    errors encountered.
    """
    run_id = str(uuid.uuid4())
    report: dict[str, Any] = {
        "run_id": run_id,
        "dry_run": dry_run,
        "threshold": similarity_threshold,
        "clusters_found": 0,
        "clusters_merged": 0,
        "clusters_skipped_large": 0,
        "memories_deprecated": 0,
        "adversarial_blocked": 0,
        "shrink_gate_blocked": 0,
        "rollback_flagged": False,
        "errors": [],
    }

    # Phase 0 — Memory preflight
    avail_mb = _read_mem_available_mb()
    if avail_mb is not None and avail_mb < MIN_AVAILABLE_MB:
        report["aborted"] = f"low_memory ({avail_mb}MB < {MIN_AVAILABLE_MB}MB)"
        logger.error(
            "Dream cycle %s: aborting — only %dMB available (need %dMB)",
            run_id[:8], avail_mb, MIN_AVAILABLE_MB,
        )
        return report

    # Phase 1 — Scroll and group by (wing, room)
    buckets = await _scroll_and_group(qdrant)
    total_points = sum(len(pts) for pts in buckets.values())
    report["total_points"] = total_points
    report["buckets"] = len(buckets)
    logger.info(
        "Dream cycle %s: %d points across %d (wing,room) buckets",
        run_id[:8], total_points, len(buckets),
    )

    # Phase 2 — Cluster within each bucket (chunked for safety)
    all_clusters: list[list[dict]] = []
    bucket_sizes: dict[str, int] = {}
    for (wing, room), points in buckets.items():
        bucket_sizes[f"{wing}/{room}"] = len(points)
        if len(points) < 2:
            continue

        # Chunk large buckets to prevent I/O saturation.
        # Shuffling rotates chunk boundaries across weekly runs,
        # giving cross-chunk convergence over 2-3 cycles.
        if len(points) > MAX_BUCKET_SIZE:
            logger.info(
                "Bucket (%s, %s): %d points — splitting into %d chunks of %d",
                wing, room, len(points),
                -(-len(points) // MAX_BUCKET_SIZE),  # ceil division
                MAX_BUCKET_SIZE,
            )
            random.shuffle(points)

        for chunk_start in range(0, len(points), MAX_BUCKET_SIZE):
            chunk = points[chunk_start:chunk_start + MAX_BUCKET_SIZE]
            if len(chunk) < 2:
                continue
            clusters = await _cluster_bucket(
                qdrant, chunk, wing, room,
                threshold=similarity_threshold,
            )
            all_clusters.extend(clusters)

    report["bucket_sizes"] = bucket_sizes

    report["clusters_found"] = len(all_clusters)
    logger.info("Dream cycle %s: found %d clusters", run_id[:8], len(all_clusters))

    # Always populate cluster report data (useful in both dry_run and live)
    report["cluster_sizes"] = _size_distribution(all_clusters)
    if dry_run:
        report["sample_clusters"] = _sample_clusters(all_clusters, n=5)

    # Phase 2b — Cross-wing similarity scan (detection only)
    # Finds memories that are similar across different wings. Creates links
    # but never merges or deprecates cross-wing. Runs in both dry_run and live.
    try:
        cross_wing = await _cross_wing_scan(
            qdrant=qdrant, db=db, buckets=buckets, run_id=run_id,
        )
        report["cross_wing_findings"] = cross_wing
        if cross_wing:
            logger.info(
                "Dream cycle %s: %d cross-wing finding(s)",
                run_id[:8], len(cross_wing),
            )
    except Exception:
        logger.warning("Cross-wing scan failed", exc_info=True)
        report["cross_wing_findings"] = []

    # Phase 3+4 — Synthesize and deprecate (live mode only)
    if not dry_run:
        all_clusters.sort(key=len, reverse=True)
        merged = 0

        for cluster in all_clusters:
            if merged >= max_merges:
                break

            if len(cluster) > max_cluster_size:
                report["clusters_skipped_large"] += 1
                logger.info(
                    "Dream cycle %s: skipping cluster of %d in %s/%s (too large)",
                    run_id[:8], len(cluster),
                    cluster[0].get("wing", "?"), cluster[0].get("room", "?"),
                )
                continue

            try:
                result = await _synthesize_and_deprecate(
                    cluster=cluster,
                    run_id=run_id,
                    qdrant=qdrant,
                    db=db,
                    router=router,
                    store=store,
                )
                merged += 1
                report["memories_deprecated"] += result["deprecated_count"]
            except SynthesisBlockedError as exc:
                # Adversarial review or shrink gate blocked this cluster.
                # Not an error — a quality gate working as intended.
                if "catastrophic shrink" in str(exc):
                    report["shrink_gate_blocked"] += 1
                else:
                    report["adversarial_blocked"] += 1
                logger.info(
                    "Dream cycle %s: cluster of %d blocked: %s",
                    run_id[:8], len(cluster), exc,
                )
            except Exception as exc:
                report["errors"].append({
                    "cluster_size": len(cluster),
                    "error": str(exc),
                })
                logger.warning(
                    "Dream cycle %s: synthesis failed for cluster of %d: %s",
                    run_id[:8], len(cluster), exc, exc_info=True,
                )

        report["clusters_merged"] = merged
        logger.info(
            "Dream cycle %s: merged %d clusters, deprecated %d memories, %d errors",
            run_id[:8], merged, report["memories_deprecated"], len(report["errors"]),
        )

        # ── Rollback flagging ──
        # If >50% of synthesis attempts were blocked, flag the run for
        # manual review. Do NOT auto-rollback.
        total_blocked = report["adversarial_blocked"] + report["shrink_gate_blocked"]
        total_attempted = merged + total_blocked
        if total_attempted > 0:
            block_rate = total_blocked / total_attempted
            if block_rate > 0.50:
                report["rollback_flagged"] = True
                logger.critical(
                    "Dream cycle %s: %.0f%% of syntheses blocked (%d/%d). "
                    "Manual review recommended. Run rollback('%s') if needed.",
                    run_id[:8], block_rate * 100,
                    total_blocked, total_attempted, run_id,
                )

    # ── Sprint 2 phases (each handles dry_run internally) ──────────────

    phase_kwargs = dict(
        qdrant=qdrant, db=db, router=router, store=store,
        run_id=run_id, dry_run=dry_run,
    )

    # Phase 5 — Link repair
    try:
        from genesis.memory.dream_link_repair import run_link_repair

        report["link_repair"] = await run_link_repair(**phase_kwargs)
    except Exception as exc:
        report["errors"].append({"phase": "link_repair", "error": str(exc)})
        logger.warning("Dream phase link_repair failed: %s", exc, exc_info=True)

    # Phase 6 — Entity resolution (dedup + contradiction detection)
    try:
        from genesis.memory.dream_entity_scan import run_entity_resolution

        report["entity_resolution"] = await run_entity_resolution(
            **phase_kwargs, buckets=buckets,
        )
    except Exception as exc:
        report["errors"].append({"phase": "entity_resolution", "error": str(exc)})
        logger.warning("Dream phase entity_resolution failed: %s", exc, exc_info=True)

    # Phase 7 — Orphan detection
    try:
        from genesis.memory.dream_orphan_detection import run_orphan_detection

        report["orphan_detection"] = await run_orphan_detection(**phase_kwargs)
    except Exception as exc:
        report["errors"].append({"phase": "orphan_detection", "error": str(exc)})
        logger.warning("Dream phase orphan_detection failed: %s", exc, exc_info=True)

    # Phase 8 — Centrality recomputation (runs even in dry_run)
    try:
        from genesis.memory.dream_centrality import run_centrality_recompute

        report["centrality"] = await run_centrality_recompute(**phase_kwargs)
    except Exception as exc:
        report["errors"].append({"phase": "centrality", "error": str(exc)})
        logger.warning("Dream phase centrality failed: %s", exc, exc_info=True)

    return report


# ── Phase 1: Scroll and Group ────────────────────────────────────────────


def _scroll_and_group_sync(
    qdrant: QdrantClient,
) -> dict[tuple[str, str], list[dict]]:
    """Scroll all episodic_memory points, group by (wing, room).

    Synchronous — runs in a thread pool via ``_scroll_and_group()`` so
    the blocking Qdrant I/O never starves the async event loop.

    Skips already-deprecated points.
    """
    from genesis.qdrant.collections import scroll_points

    buckets: dict[tuple[str, str], list[dict]] = defaultdict(list)
    offset: str | None = None

    while True:
        points, next_offset = scroll_points(
            qdrant, collection=COLLECTION, limit=1000, offset=offset,
        )
        for point in points:
            payload = point.get("payload", {})
            # Skip already-deprecated memories
            if payload.get("deprecated") is True:
                continue
            wing = payload.get("wing") or "general"
            room = payload.get("room") or "uncategorized"
            buckets[(wing, room)].append({
                "id": point["id"],
                "payload": payload,
            })

        if next_offset is None or not points:
            break
        offset = next_offset

    return buckets


async def _scroll_and_group(
    qdrant: QdrantClient,
) -> dict[tuple[str, str], list[dict]]:
    """Async wrapper — offloads blocking Qdrant scroll I/O to thread pool."""
    return await asyncio.to_thread(_scroll_and_group_sync, qdrant)


async def _cross_wing_scan(
    *,
    qdrant: QdrantClient,
    db: aiosqlite.Connection,
    buckets: dict[tuple[str, str], list[dict]],
    run_id: str,
    top_n_per_wing: int = 20,
    similarity_threshold: float = 0.90,
) -> list[dict]:
    """Scan for similar memories across different wings.

    Takes the top-N highest-confidence memories from each wing and searches
    for cross-wing matches via Qdrant similarity search. Creates links for
    findings but never merges or deprecates.

    Returns a list of finding dicts for the report.
    """
    from genesis.qdrant.collections import search as qdrant_search

    # Group points by wing (collapse rooms within each wing)
    wing_points: dict[str, list[dict]] = defaultdict(list)
    for (_wing, _room), points in buckets.items():
        wing_points[_wing].extend(points)

    wings = list(wing_points.keys())
    if len(wings) < 2:
        return []

    # Take top-N per wing by confidence
    wing_top: dict[str, list[dict]] = {}
    for wing, points in wing_points.items():
        sorted_pts = sorted(
            points,
            key=lambda p: p.get("payload", {}).get("confidence", 0.0),
            reverse=True,
        )
        wing_top[wing] = sorted_pts[:top_n_per_wing]

    # Get vectors for top points
    all_top_ids = [p["id"] for pts in wing_top.values() for p in pts]
    vectors = await asyncio.to_thread(
        _batch_get_vectors, qdrant, all_top_ids,
    )

    findings: list[dict] = []
    link_count = 0

    for i, wing_a in enumerate(wings):
        for wing_b in wings[i + 1:]:
            # Search wing_b for memories similar to wing_a's top memories
            for point in wing_top.get(wing_a, []):
                vec = vectors.get(point["id"])
                if not vec:
                    continue

                try:
                    hits = await asyncio.to_thread(
                        qdrant_search,
                        qdrant,
                        collection=COLLECTION,
                        query_vector=vec,
                        limit=5,
                        wing=wing_b,
                    )
                except Exception:
                    continue

                for hit in hits:
                    score = hit.get("score", 0.0)
                    if score < similarity_threshold:
                        continue

                    content_a = point.get("payload", {}).get("content", "")[:200]
                    content_b = hit.get("payload", {}).get("content", "")[:200]

                    # Cosine similarity alone cannot detect contradiction —
                    # high cosine means similar, not contradictory. Use
                    # related_to for all cross-wing findings. Contradiction
                    # detection would require an LLM semantic check.
                    link_type = "related_to"

                    # Create link between cross-wing memories (skip if exists)
                    try:
                        from genesis.db.crud import memory_links
                        # Check for existing link to avoid UNIQUE constraint
                        # failures on subsequent runs
                        existing = await db.execute(
                            "SELECT 1 FROM memory_links "
                            "WHERE source_id = ? AND target_id = ? LIMIT 1",
                            (point["id"], hit["id"]),
                        )
                        if not await existing.fetchone():
                            await memory_links.create(
                                db,
                                source_id=point["id"],
                                target_id=hit["id"],
                                link_type=link_type,
                                strength=round(score, 4),
                                created_at=datetime.now(UTC).isoformat(),
                            )
                            link_count += 1
                    except Exception:
                        logger.debug(
                            "Cross-wing link creation failed",
                            exc_info=True,
                        )

                    findings.append({
                        "wing_a": wing_a,
                        "wing_b": wing_b,
                        "memory_a": point["id"][:8],
                        "memory_b": hit["id"][:8],
                        "cosine": round(score, 3),
                        "link_type": link_type,
                        "content_a_preview": content_a,
                        "content_b_preview": content_b,
                    })

    if link_count:
        try:
            from genesis.memory.graph import invalidate_graph_cache
            invalidate_graph_cache()
        except Exception:
            pass

    return findings



# ── Phase 2: Cluster ────────────────────────────────────────────────────


def _cluster_bucket_sync(
    qdrant: QdrantClient,
    points: list[dict],
    wing: str,
    room: str,
    *,
    threshold: float,
) -> list[list[dict]]:
    """Find connected components of similar memories within a (wing, room) bucket.

    Synchronous — runs in a thread pool via ``_cluster_bucket()`` so the
    blocking Qdrant search I/O never starves the async event loop.

    For each point, searches Qdrant for neighbors above threshold,
    then extracts connected components via union-find.
    """
    from genesis.qdrant.collections import search

    uf = _UnionFind()
    point_map = {p["id"]: p for p in points}

    # Batch-retrieve all vectors for this bucket in one call
    # (avoids N sync blocking calls to Qdrant)
    vector_map = _batch_get_vectors(qdrant, [p["id"] for p in points])

    # Build similarity graph
    n_points = len(points)
    for idx, point in enumerate(points):
        pid = point["id"]
        vec = vector_map.get(pid)
        if vec is None:
            continue
        try:
            neighbors = search(
                qdrant,
                collection=COLLECTION,
                query_vector=vec,
                limit=20,
                wing=wing,
                room=room,
            )
        except Exception:
            logger.debug("Could not search neighbors for %s", pid)
            continue

        for neighbor in neighbors:
            nid = neighbor["id"]
            score = neighbor["score"]
            if nid == pid:
                continue
            if nid not in point_map:
                continue  # Different bucket or deprecated
            if score >= threshold:
                uf.union(pid, nid)

        if (idx + 1) % 100 == 0:
            logger.info(
                "Bucket (%s, %s): searched %d/%d points",
                wing, room, idx + 1, n_points,
            )

    # Extract components with >= 2 members
    clusters: list[list[dict]] = []
    for _root, members in uf.components().items():
        if len(members) >= 2:
            cluster = [point_map[m] for m in members if m in point_map]
            if len(cluster) >= 2:
                # Tag wing/room for downstream use
                for item in cluster:
                    item["wing"] = wing
                    item["room"] = room
                clusters.append(cluster)

    return clusters


async def _cluster_bucket(
    qdrant: QdrantClient,
    points: list[dict],
    wing: str,
    room: str,
    *,
    threshold: float,
) -> list[list[dict]]:
    """Async wrapper — offloads blocking Qdrant search I/O to thread pool."""
    return await asyncio.to_thread(
        _cluster_bucket_sync, qdrant, points, wing, room, threshold=threshold,
    )


def _batch_get_vectors(
    qdrant: QdrantClient, point_ids: list[str],
) -> dict[str, list[float]]:
    """Batch-retrieve vectors — delegates to shared Qdrant utility."""
    from genesis.qdrant.collections import batch_retrieve_vectors

    return batch_retrieve_vectors(qdrant, point_ids, collection=COLLECTION)


def _get_vector(qdrant: QdrantClient, point_id: str) -> list[float]:
    """Retrieve a single point's vector from Qdrant (used by tests)."""
    result = qdrant.retrieve(
        collection_name=COLLECTION,
        ids=[point_id],
        with_vectors=True,
    )
    if not result:
        raise ValueError(f"Point {point_id} not found in {COLLECTION}")
    return result[0].vector


# ── Phase 3+4: Synthesize and Deprecate ──────────────────────────────────


async def _synthesize_and_deprecate(
    *,
    cluster: list[dict],
    run_id: str,
    qdrant: QdrantClient,
    db: aiosqlite.Connection,
    router: Router,
    store: MemoryStore,
) -> dict[str, Any]:
    """Synthesize a cluster into one canonical memory, deprecate originals."""
    wing = cluster[0]["wing"]
    room = cluster[0]["room"]
    original_ids = [item["id"] for item in cluster]

    # Build synthesis prompt
    prompt = _build_synthesis_prompt(cluster, wing, room)
    messages = [{"role": "user", "content": prompt}]

    # LLM synthesis via router
    result = await router.route_call(CALL_SITE_ID, messages)
    if not result.success:
        raise RuntimeError(f"LLM synthesis failed: {result.error}")

    synthesis = _parse_synthesis_response(result.content, wing, room)

    # ── Adversarial review ──
    # A different-provider LLM reviews the synthesis for information loss.
    # Fail-safe: if review fails or errors, block this cluster.
    from genesis.memory.adversarial_review import check_synthesis_faithfulness
    adversarial_verdict = await check_synthesis_faithfulness(
        router=router,
        originals=[
            {"content": item["payload"].get("content", ""),
             "confidence": item["payload"].get("confidence", 0.5)}
            for item in cluster
        ],
        synthesis_text=synthesis.get("content", ""),
    )
    if not adversarial_verdict.passed:
        raise SynthesisBlockedError(
            missing=adversarial_verdict.missing,
            error=adversarial_verdict.error,
        )

    # ── Catastrophic-shrink gate ──
    # Block synthesis if it's <50% the combined length of originals.
    originals_length = sum(
        len(item["payload"].get("content", "")) for item in cluster
    )
    synthesis_length = len(synthesis.get("content", ""))
    if originals_length > 0 and synthesis_length < originals_length * 0.5:
        raise SynthesisBlockedError(
            error=(
                f"catastrophic shrink: synthesis {synthesis_length} chars "
                f"vs originals {originals_length} chars "
                f"({synthesis_length / originals_length:.0%})"
            ),
        )

    # Merge tags from originals + synthesis
    all_tags = set()
    for item in cluster:
        for tag in item["payload"].get("tags", []):
            if tag != "deprecated":
                all_tags.add(tag)
    for tag in synthesis.get("tags", []):
        all_tags.add(tag)
    all_tags.add("synthesized")
    all_tags.add(f"dream_cycle_run_id:{run_id}")

    # Store synthesized memory via MemoryStore.
    # Use median confidence with a ceiling to prevent confidence inflation
    # through consolidation cycles. max() lets a single high-confidence
    # memory inflate the synthesis; median is resistant to outliers.
    # Ceiling prevents unbounded growth across dream cycle runs.
    source_confidences = [
        item["payload"].get("confidence", 0.5)
        for item in cluster
    ]
    median_confidence = statistics.median(source_confidences)
    _CONFIDENCE_CEILING = 0.85

    new_memory_id = await store.store(
        synthesis["content"],
        source="dream_cycle",
        memory_type="episodic",
        tags=sorted(all_tags),
        confidence=min(median_confidence, _CONFIDENCE_CEILING),
        source_pipeline="dream_cycle",
        wing=synthesis.get("wing", wing),
        room=synthesis.get("room", room),
        auto_link=False,  # We create provenance links explicitly below
    )

    # Set synthesized_from on the new memory's Qdrant payload
    from genesis.qdrant.collections import update_payload
    update_payload(
        qdrant,
        collection=COLLECTION,
        point_id=new_memory_id,
        payload={"synthesized_from": original_ids},
    )

    # Stamp synthesis memory with run_id for rollback (prefixed to
    # distinguish from deprecated originals in the same column)
    await db.execute(
        "UPDATE memory_metadata SET dream_cycle_run_id = ? WHERE memory_id = ?",
        (f"synthesis:{run_id}", new_memory_id),
    )

    # Deprecate originals
    deprecated_count = 0
    for original_id in original_ids:
        try:
            # Qdrant: mark as deprecated
            update_payload(
                qdrant,
                collection=COLLECTION,
                point_id=original_id,
                payload={
                    "deprecated": True,
                    "synthesized_into": new_memory_id,
                },
            )
            # SQLite: mark as deprecated
            await db.execute(
                "UPDATE memory_metadata SET deprecated = 1, "
                "dream_cycle_run_id = ? WHERE memory_id = ?",
                (run_id, original_id),
            )
            deprecated_count += 1
        except Exception:
            logger.warning(
                "Failed to deprecate memory %s", original_id, exc_info=True,
            )
    await db.commit()

    # Create links from synthesis to originals
    if store.linker:
        for original_id in original_ids:
            try:
                from genesis.db.crud import memory_links as links_crud
                now_iso = datetime.now(UTC).isoformat()
                await links_crud.create(
                    db,
                    source_id=new_memory_id,
                    target_id=original_id,
                    link_type="extends",
                    strength=1.0,
                    created_at=now_iso,
                )
            except Exception:
                logger.debug(
                    "Dream cycle: link %s → %s failed",
                    new_memory_id, original_id, exc_info=True,
                )

    logger.info(
        "Dream cycle: synthesized %d memories into %s (%s/%s)",
        len(cluster), new_memory_id[:8], wing, room,
    )

    return {
        "new_memory_id": new_memory_id,
        "deprecated_count": deprecated_count,
        "original_ids": original_ids,
    }


# ── Rollback ─────────────────────────────────────────────────────────────


async def rollback(
    run_id: str,
    *,
    qdrant: QdrantClient,
    db: aiosqlite.Connection,
) -> dict[str, Any]:
    """Reverse a dream cycle run.

    1. Find all memories deprecated by this run_id
    2. Clear their deprecated flag (Qdrant + SQLite)
    3. Find synthesized memories created by this run (by tag)
    4. Hard-delete the syntheses (they're derived, not original data)
    """
    from genesis.qdrant.collections import update_payload

    report: dict[str, Any] = {
        "run_id": run_id,
        "restored": 0,
        "syntheses_deleted": 0,
        "errors": [],
    }

    # 1. Restore deprecated originals in SQLite
    cursor = await db.execute(
        "SELECT memory_id FROM memory_metadata "
        "WHERE dream_cycle_run_id = ? AND deprecated = 1",
        (run_id,),
    )
    deprecated_ids = [row[0] for row in await cursor.fetchall()]

    for mid in deprecated_ids:
        try:
            await db.execute(
                "UPDATE memory_metadata SET deprecated = 0, "
                "dream_cycle_run_id = NULL WHERE memory_id = ?",
                (mid,),
            )
            update_payload(
                qdrant,
                collection=COLLECTION,
                point_id=mid,
                payload={"deprecated": False, "synthesized_into": None, "merged_into": None},
            )
            report["restored"] += 1
        except Exception as exc:
            report["errors"].append({"memory_id": mid, "error": str(exc)})

    await db.commit()

    # 2. Delete synthesized memories created by this run
    # Syntheses are stamped with "synthesis:{run_id}" in dream_cycle_run_id
    cursor = await db.execute(
        "SELECT memory_id FROM memory_metadata WHERE dream_cycle_run_id = ?",
        (f"synthesis:{run_id}",),
    )
    synthesis_ids = [row[0] for row in await cursor.fetchall()]

    for sid in synthesis_ids:
        try:
            from genesis.qdrant.collections import delete_point
            delete_point(qdrant, collection=COLLECTION, point_id=sid)
            await db.execute(
                "DELETE FROM memory_metadata WHERE memory_id = ?", (sid,),
            )
            await db.execute(
                "DELETE FROM memory_fts WHERE memory_id = ?", (sid,),
            )
            await db.execute(
                "DELETE FROM memory_links WHERE source_id = ? OR target_id = ?",
                (sid, sid),
            )
            report["syntheses_deleted"] += 1
        except Exception as exc:
            report["errors"].append({"synthesis_id": sid, "error": str(exc)})

    await db.commit()

    # Invalidate graph cache since we deleted links
    if report["syntheses_deleted"] > 0:
        try:
            from genesis.memory.graph import invalidate_graph_cache
            invalidate_graph_cache()
        except ImportError:
            pass

    logger.info(
        "Dream cycle rollback %s: restored %d, deleted %d syntheses, %d errors",
        run_id[:8], report["restored"], report["syntheses_deleted"],
        len(report["errors"]),
    )
    return report


# ── Startup Integrity Check ──────────────────────────────────────────────


async def check_incomplete_runs(
    db: aiosqlite.Connection,
) -> list[dict[str, Any]]:
    """Detect dream cycle runs that may have left inconsistent state.

    If the process was killed mid-merge, some originals may be deprecated
    in SQLite without a corresponding synthesis, or vice versa. This check
    runs at startup and logs warnings — it does NOT auto-rollback (that
    needs user confirmation).

    Returns list of {run_id, deprecated_count} for suspicious runs.
    """
    try:
        cursor = await db.execute(
            "SELECT dream_cycle_run_id, COUNT(*) as cnt "
            "FROM memory_metadata "
            "WHERE deprecated = 1 AND dream_cycle_run_id IS NOT NULL "
            "AND dream_cycle_run_id NOT LIKE 'synthesis:%' "
            "GROUP BY dream_cycle_run_id"
        )
        rows = await cursor.fetchall()
    except Exception:
        logger.debug("Dream cycle integrity check skipped (query failed)", exc_info=True)
        return []

    suspicious: list[dict[str, Any]] = []
    for row in rows:
        run_id = row[0]
        count = row[1]
        # Check if synthesis exists for this run
        try:
            synth_cursor = await db.execute(
                "SELECT COUNT(*) FROM memory_metadata "
                "WHERE dream_cycle_run_id = ?",
                (f"synthesis:{run_id}",),
            )
            synth_count = (await synth_cursor.fetchone())[0]
        except Exception:
            synth_count = -1

        if synth_count == 0:
            logger.warning(
                "Dream cycle run %s: %d deprecated memories with NO synthesis — "
                "possible incomplete run. Use dream_cycle.rollback('%s') to restore.",
                run_id[:8], count, run_id,
            )
            suspicious.append({"run_id": run_id, "deprecated_count": count})

    if suspicious:
        logger.warning(
            "Found %d potentially incomplete dream cycle run(s)", len(suspicious),
        )
    return suspicious


# ── Prompt and Parsing ───────────────────────────────────────────────────

_SYNTHESIS_PROMPT = """\
You are synthesizing a cluster of related memories into a single canonical record.

These memories are all tagged wing={wing}, room={room}. They share high semantic \
similarity (cosine >= 0.87). Your job is to produce ONE memory that is strictly \
more informative than any individual original, preserving all unique facts while \
eliminating redundancy.

Input memories ({n} total):
{memories}

Output JSON (no markdown fences, just raw JSON):
{{
  "content": "<synthesized content — complete, self-contained>",
  "tags": ["<merged relevant tags — deduplicated>"],
  "confidence": <float 0-1, max of inputs as baseline>,
  "memory_class": "<fact|reference|procedure|insight>",
  "wing": "<wing>",
  "room": "<room>",
  "synthesis_notes": "<why these were merged, what was dropped>"
}}

Rules:
- Never invent facts not present in the inputs
- Preserve all unique details — err on the side of keeping too much
- If memories contradict, note the contradiction explicitly in content
- If memories represent temporal evolution (X was true, then Y), preserve the timeline
- If one memory has much higher confidence, it likely supersedes the others — note this\
"""


def _build_synthesis_prompt(
    cluster: list[dict], wing: str, room: str,
) -> str:
    """Build the synthesis prompt from cluster memories."""
    memory_blocks = []
    for i, item in enumerate(cluster, 1):
        payload = item["payload"]
        confidence = payload.get("confidence", 0.5)
        source = payload.get("source", "unknown")
        created = payload.get("created_at", "unknown")
        content = payload.get("content", "")
        memory_blocks.append(
            f"--- Memory {i} (confidence {confidence}, source {source}, "
            f"created {created}) ---\n{content}"
        )
    return _SYNTHESIS_PROMPT.format(
        wing=wing,
        room=room,
        n=len(cluster),
        memories="\n\n".join(memory_blocks),
    )


def _parse_synthesis_response(
    response: str, default_wing: str, default_room: str,
) -> dict[str, Any]:
    """Parse the LLM's JSON synthesis response.

    Falls back gracefully if the response isn't valid JSON — uses the
    raw response as content with defaults for other fields.
    """
    # Strip markdown fences if present
    text = response.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        # Remove first and last fence lines
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    try:
        data = json.loads(text)
        # Validate required fields
        if "content" not in data or not data["content"]:
            raise ValueError("Missing 'content' field")
        return {
            "content": data["content"],
            "tags": data.get("tags", []),
            "confidence": data.get("confidence", 0.8),
            "memory_class": data.get("memory_class", "fact"),
            "wing": data.get("wing", default_wing),
            "room": data.get("room", default_room),
            "synthesis_notes": data.get("synthesis_notes", ""),
        }
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("Failed to parse synthesis JSON: %s", exc)
        # Fallback: use raw response as content
        return {
            "content": response,
            "tags": [],
            "confidence": 0.8,
            "memory_class": "fact",
            "wing": default_wing,
            "room": default_room,
            "synthesis_notes": f"JSON parse failed: {exc}",
        }


# ── Helpers ──────────────────────────────────────────────────────────────


def _size_distribution(clusters: list[list[dict]]) -> dict[str, int]:
    """Categorize clusters by size for dry-run reporting."""
    dist: dict[str, int] = {"2-3": 0, "4-5": 0, "6-10": 0, "11+": 0}
    for c in clusters:
        n = len(c)
        if n <= 3:
            dist["2-3"] += 1
        elif n <= 5:
            dist["4-5"] += 1
        elif n <= 10:
            dist["6-10"] += 1
        else:
            dist["11+"] += 1
    return dist


def _sample_clusters(
    clusters: list[list[dict]], *, n: int = 5,
) -> list[dict]:
    """Return summaries of the first N clusters for dry-run review."""
    samples = []
    for cluster in clusters[:n]:
        samples.append({
            "size": len(cluster),
            "wing": cluster[0].get("wing", "?"),
            "room": cluster[0].get("room", "?"),
            "sample_content": [
                item["payload"].get("content", "")[:100]
                for item in cluster[:3]
            ],
        })
    return samples
