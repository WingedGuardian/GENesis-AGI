"""DRIFT-style multi-mode retrieval for Genesis memory.

Implements a two-phase retrieval approach inspired by Microsoft GraphRAG's
DRIFT query mode: broad context identification followed by targeted local
drill-down. This produces better recall for complex/ambiguous queries than
single-pass hybrid search.

Phase 1 (Global Primer): Scan across wings/rooms to identify the most
    relevant memory clusters for the query.
Phase 2 (Local Drill-Down): Focused vector + FTS5 search within the
    identified clusters, expanded via graph traversal.
Phase 3 (Combine): RRF fusion of global and local results with
    appropriate weighting.

Usage:
    from genesis.memory.drift import drift_recall

    results = await drift_recall(
        query="why did we switch to ephemeral ego sessions?",
        db=db,
        qdrant_client=qdrant,
        embedding_provider=embeddings,
    )

Designed as a standalone function — easy to swap for Graphiti if the
prototype spike proves superior.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import UTC, datetime

import aiosqlite
from qdrant_client import QdrantClient

from genesis.db.crud import memory as memory_crud
from genesis.memory.activation import compute_activation
from genesis.memory.embeddings import EmbeddingProvider, EmbeddingUnavailableError
from genesis.memory.graph import traverse as graph_traverse
from genesis.memory.intent import classify_intent
from genesis.memory.types import RetrievalResult
from genesis.qdrant import collections as qdrant_ops

logger = logging.getLogger(__name__)


def _coalesce(value: object, default: object) -> object:
    """Return *default* if *value* is ``None`` (null-coalescing for DB rows).

    Unlike ``or``, this preserves valid falsy values such as ``0`` and ``""``
    that SQLite rows may legitimately contain.
    """
    return default if value is None else value


# Weights for combining global vs local results in final RRF
_GLOBAL_WEIGHT = 0.3
_LOCAL_WEIGHT = 0.7
_RRF_K = 60


def _rrf_fuse(
    ranked_lists: list[list[str]],
    *,
    k: int = _RRF_K,
) -> dict[str, float]:
    """Reciprocal Rank Fusion. Returns {memory_id: fused_score}."""
    scores: dict[str, float] = {}
    for ranked in ranked_lists:
        for rank, mid in enumerate(ranked, 1):
            scores[mid] = scores.get(mid, 0.0) + 1.0 / (k + rank)
    return scores


async def _global_primer(
    query: str,
    *,
    db: aiosqlite.Connection,
    qdrant_client: QdrantClient,
    embedding_provider: EmbeddingProvider,
    source_collections: list[str],
    global_limit: int = 20,
    exclude_subsystems: list[str] | None = None,
    include_only_subsystems: list[str] | None = None,
) -> tuple[list[str], str | None, str | None]:
    """Phase 1: Broad scan to identify relevant wing/room clusters.

    Returns:
        (ranked_memory_ids, best_wing, best_room)
    """
    # FTS5 broad search — no wing/room filter, cast a wide net
    fts_results = await memory_crud.search_ranked(
        db, query=query, limit=global_limit,
        exclude_subsystems=exclude_subsystems,
        include_only_subsystems=include_only_subsystems,
    )
    fts_ids = [r["memory_id"] for r in fts_results]

    # Vector search — broad, across all collections
    vector_ids: list[str] = []
    try:
        query_vector = await embedding_provider.embed(query)
        for collection in source_collections:
            hits = qdrant_ops.search(
                qdrant_client,
                collection=collection,
                query_vector=query_vector,
                limit=global_limit,
                exclude_subsystems=exclude_subsystems,
                include_only_subsystems=include_only_subsystems,
            )
            vector_ids.extend(hit["id"] for hit in hits)
    except (EmbeddingUnavailableError, Exception) as e:
        logger.debug("DRIFT global primer: vector search unavailable: %s", e)

    # Fuse to get top global results
    ranked_lists = [v for v in [vector_ids, fts_ids] if v]
    if not ranked_lists:
        return [], None, None

    fused = _rrf_fuse(ranked_lists)
    top_ids = sorted(fused, key=fused.get, reverse=True)[:global_limit]  # type: ignore[arg-type]

    # Identify dominant wing/room from global results
    best_wing, best_room = await _identify_clusters(top_ids, db=db)

    return top_ids, best_wing, best_room


async def _identify_clusters(
    memory_ids: list[str],
    *,
    db: aiosqlite.Connection,
) -> tuple[str | None, str | None]:
    """Identify the dominant wing and room from a set of memory IDs.

    Counts wing/room occurrences in the memory metadata and returns
    the most frequent pair. Returns (None, None) if no metadata available.
    """
    if not memory_ids:
        return None, None

    wing_counts: Counter[str] = Counter()
    room_counts: Counter[str] = Counter()

    # Batch fetch metadata from SQLite
    placeholders = ",".join("?" * len(memory_ids))
    query = f"""
        SELECT memory_id, wing, room FROM memory_metadata
        WHERE memory_id IN ({placeholders}) AND wing IS NOT NULL
    """
    async with db.execute(query, memory_ids) as cursor:
        async for row in cursor:
            wing = row[1]
            room = row[2]
            if wing:
                wing_counts[wing] += 1
            if room:
                room_counts[room] += 1

    best_wing = wing_counts.most_common(1)[0][0] if wing_counts else None
    best_room = room_counts.most_common(1)[0][0] if room_counts else None

    return best_wing, best_room


async def _local_drilldown(
    query: str,
    *,
    db: aiosqlite.Connection,
    qdrant_client: QdrantClient,
    embedding_provider: EmbeddingProvider,
    source_collections: list[str],
    wing: str | None,
    room: str | None,
    global_ids: list[str],
    local_limit: int = 15,
    exclude_subsystems: list[str] | None = None,
    include_only_subsystems: list[str] | None = None,
) -> list[str]:
    """Phase 2: Focused search within the identified cluster.

    Searches with wing/room filters and expands via graph traversal
    from top global results.
    """
    local_ids: list[str] = []

    # Scoped FTS5 search (room filter via tag match if available)
    fts_query = query
    if wing:
        # FTS5 can filter by tags field which contains wing info
        fts_results = await memory_crud.search_ranked(
            db, query=fts_query, collection="episodic_memory", limit=local_limit,
            exclude_subsystems=exclude_subsystems,
            include_only_subsystems=include_only_subsystems,
        )
        # Filter results by wing in post-processing (FTS5 doesn't support wing filter)
        fts_ids = [r["memory_id"] for r in fts_results]
        if wing:
            # Verify wing membership
            placeholders = ",".join("?" * len(fts_ids))
            if fts_ids:
                wing_query = f"""
                    SELECT memory_id FROM memory_metadata
                    WHERE memory_id IN ({placeholders}) AND wing = ?
                """
                async with db.execute(wing_query, [*fts_ids, wing]) as cursor:
                    fts_ids = [row[0] async for row in cursor]
        local_ids.extend(fts_ids)

    # Scoped vector search with wing filter
    try:
        query_vector = await embedding_provider.embed(query)
        for collection in source_collections:
            hits = qdrant_ops.search(
                qdrant_client,
                collection=collection,
                query_vector=query_vector,
                limit=local_limit,
                wing=wing,
                room=room,
                exclude_subsystems=exclude_subsystems,
                include_only_subsystems=include_only_subsystems,
            )
            local_ids.extend(hit["id"] for hit in hits)
    except (EmbeddingUnavailableError, Exception) as e:
        logger.debug("DRIFT local drilldown: vector search unavailable: %s", e)

    # Graph expansion: traverse 1-hop from top global results
    expansion_roots = global_ids[:5]  # Top 5 global results
    for root_id in expansion_roots:
        try:
            traversal = await graph_traverse(
                db, root_id, max_depth=1, min_strength=0.5
            )
            for node in traversal.nodes:
                if node.memory_id not in local_ids:
                    local_ids.append(node.memory_id)
        except Exception:
            # Graph traversal is best-effort; don't fail the query
            continue

    return local_ids


async def drift_recall(
    query: str,
    *,
    db: aiosqlite.Connection,
    qdrant_client: QdrantClient,
    embedding_provider: EmbeddingProvider,
    source: str = "episodic",
    limit: int = 10,
    min_activation: float = 0.0,
    include_subsystem: bool | list[str] = False,
    only_subsystem: str | list[str] | None = None,
) -> list[RetrievalResult]:
    """DRIFT multi-mode retrieval: global primer → local drill-down → combine.

    Args:
        query: Search query string.
        db: SQLite database connection.
        qdrant_client: Qdrant vector store client.
        embedding_provider: Embedding generation service.
        source: Which collections to search ("episodic", "knowledge", "both").
        limit: Maximum results to return.
        min_activation: Minimum activation score threshold.
        include_subsystem: Subsystem-filter additive mode. ``False`` (default)
            excludes ego/triage/reflection writes; ``True`` returns
            everything; a list adds named subsystems alongside user
            content. Mutually exclusive with ``only_subsystem``.
        only_subsystem: Subsystem-filter replace mode. Return ONLY rows
            tagged with the named subsystem(s); user content excluded.
            Used by ego's own self-recall.

    Returns:
        List of RetrievalResult objects, ranked by combined DRIFT score.
    """
    from genesis.memory.retrieval import (
        _SOURCE_TO_COLLECTIONS,
        _resolve_subsystem_filter,
    )

    source_collections = _SOURCE_TO_COLLECTIONS.get(source, ["episodic_memory"])

    exclude_subsystems, include_only_subsystems = _resolve_subsystem_filter(
        include_subsystem, only_subsystem,
    )

    # Classify query intent for metadata enrichment
    intent = classify_intent(query)

    # Phase 1: Global primer — identify relevant clusters
    global_ids, best_wing, best_room = await _global_primer(
        query,
        db=db,
        qdrant_client=qdrant_client,
        embedding_provider=embedding_provider,
        source_collections=source_collections,
        exclude_subsystems=exclude_subsystems,
        include_only_subsystems=include_only_subsystems,
    )

    if not global_ids:
        # Fallback: no results at all
        return []

    # Phase 2: Local drill-down — focused search in identified cluster
    local_ids = await _local_drilldown(
        query,
        db=db,
        qdrant_client=qdrant_client,
        embedding_provider=embedding_provider,
        source_collections=source_collections,
        wing=best_wing,
        room=best_room,
        global_ids=global_ids,
        exclude_subsystems=exclude_subsystems,
        include_only_subsystems=include_only_subsystems,
    )

    # Phase 3: Combine — weighted RRF fusion
    # Global results get lower weight, local results get higher weight
    # We repeat local_ids to give them more RRF influence
    ranked_lists = [global_ids]
    if local_ids:
        ranked_lists.append(local_ids)
        # Double-count local for weighting effect
        ranked_lists.append(local_ids)

    fused_scores = _rrf_fuse(ranked_lists)

    # Phase 1.5e: drop candidates past their bitemporal invalid_at.
    # Both _global_primer and _local_drilldown use search_ranked (which
    # filters in-SQL) AND qdrant_ops.search (which doesn't see invalid_at),
    # so vector candidates can still leak in. Batched lookup applies the
    # same filter once over the fused set. Wrapped — DB failure here
    # degrades to "no expiry filter" rather than crash drift recall.
    from genesis.memory.retrieval import _expired_candidate_ids
    try:
        expired = await _expired_candidate_ids(db, set(fused_scores.keys()))
    except Exception:
        logger.warning(
            "drift invalid_at filter failed, returning unfiltered",
            exc_info=True,
        )
        expired = set()
    if expired:
        for mid in expired:
            fused_scores.pop(mid, None)

    # Rank by fused score
    all_ids = sorted(fused_scores, key=fused_scores.get, reverse=True)  # type: ignore[arg-type]

    # Compute activation scores and build results
    results: list[RetrievalResult] = []
    now_iso = datetime.now(UTC).isoformat()

    for mid in all_ids:
        if len(results) >= limit:
            break

        # Fetch memory metadata
        row = await memory_crud.get_by_id(db, mid)
        if not row:
            continue

        # Compute activation — use _coalesce for NULL-safe defaults because
        # dict.get() only applies the default for *missing* keys, not for
        # keys present with None values (which SQLite rows may contain).
        activation = compute_activation(
            confidence=_coalesce(row.get("confidence"), 0.5),
            created_at=_coalesce(row.get("created_at"), now_iso),
            retrieved_count=_coalesce(row.get("retrieved_count"), 0),
            link_count=_coalesce(row.get("link_count"), 0),
            source=_coalesce(row.get("source_type"), "unknown"),
            tags=row.get("tags", "").split(",") if row.get("tags") else [],
            now=now_iso,
            memory_class=_coalesce(row.get("memory_class"), "fact"),
        )

        if activation.final_score < min_activation:
            continue

        # Determine rank positions
        vector_rank = None
        fts_rank = None
        if mid in global_ids:
            vector_rank = global_ids.index(mid) + 1
        if mid in local_ids:
            fts_rank = local_ids.index(mid) + 1

        results.append(
            RetrievalResult(
                memory_id=mid,
                content=row.get("content", ""),
                source=row.get("source_type", "unknown"),
                memory_type=row.get("memory_type", "episodic"),
                score=fused_scores[mid],
                vector_rank=vector_rank,
                fts_rank=fts_rank,
                activation_score=activation.final_score,
                payload=row,
                source_session_id=row.get("source_session_id"),
                source_pipeline="drift",
                memory_class=row.get("memory_class", "fact"),
                query_intent=intent.category,
                intent_confidence=intent.confidence,
            )
        )

    return results
