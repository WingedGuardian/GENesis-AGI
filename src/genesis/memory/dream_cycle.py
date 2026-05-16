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
import uuid
from collections import defaultdict
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import aiosqlite
    from qdrant_client import QdrantClient

    from genesis.memory.store import MemoryStore
    from genesis.routing.router import Router

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

    if dry_run:
        report["cluster_sizes"] = _size_distribution(all_clusters)
        report["sample_clusters"] = _sample_clusters(all_clusters, n=5)
        logger.info("Dream cycle %s: DRY RUN — no changes written", run_id[:8])
        return report

    # Phase 3+4 — Synthesize and deprecate (live mode)
    # Sort by descending size so highest-redundancy clusters merge first
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
    return report


# ── Phase 1: Scroll and Group ────────────────────────────────────────────


async def _scroll_and_group(
    qdrant: QdrantClient,
) -> dict[tuple[str, str], list[dict]]:
    """Scroll all episodic_memory points, group by (wing, room).

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


# ── Phase 2: Cluster ────────────────────────────────────────────────────


async def _cluster_bucket(
    qdrant: QdrantClient,
    points: list[dict],
    wing: str,
    room: str,
    *,
    threshold: float,
) -> list[list[dict]]:
    """Find connected components of similar memories within a (wing, room) bucket.

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

        # Yield to the event loop periodically so other coroutines
        # (health probes, scheduler heartbeats) stay alive.
        if (idx + 1) % _YIELD_EVERY == 0:
            if (idx + 1) % 100 == 0:
                logger.info(
                    "Bucket (%s, %s): searched %d/%d points",
                    wing, room, idx + 1, n_points,
                )
            await asyncio.sleep(0)

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


def _batch_get_vectors(
    qdrant: QdrantClient, point_ids: list[str],
) -> dict[str, list[float]]:
    """Batch-retrieve vectors for all points in one Qdrant call.

    Returns {point_id: vector}. Missing points are silently omitted.
    Batches in chunks of 100 to avoid oversized requests.
    """
    vector_map: dict[str, list[float]] = {}
    batch_size = 100
    for i in range(0, len(point_ids), batch_size):
        batch_ids = point_ids[i:i + batch_size]
        try:
            results = qdrant.retrieve(
                collection_name=COLLECTION,
                ids=batch_ids,
                with_vectors=True,
            )
            for r in results:
                vector_map[str(r.id)] = r.vector
        except Exception:
            logger.warning(
                "Failed to retrieve vectors for batch %d-%d",
                i, i + len(batch_ids), exc_info=True,
            )
    return vector_map


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

    # Store synthesized memory via MemoryStore
    max_confidence = max(
        item["payload"].get("confidence", 0.5)
        for item in cluster
    )
    new_memory_id = await store.store(
        synthesis["content"],
        source="dream_cycle",
        memory_type="episodic",
        tags=sorted(all_tags),
        confidence=max(max_confidence, synthesis.get("confidence", 0.8)),
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
                payload={"deprecated": False, "synthesized_into": None},
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
