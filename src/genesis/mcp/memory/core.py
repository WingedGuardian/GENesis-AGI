"""Core memory tools: recall, store, extract, proactive, core_facts, stats, expand."""

from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import UTC, datetime

from genesis.memory.activation import compute_activation
from genesis.memory.graph import traverse as graph_traverse

from ..memory import mcp


def _memory_mod():
    import genesis.mcp.memory_mcp as memory_mod

    return memory_mod

logger = logging.getLogger(__name__)


def _increment_retrieved(qdrant, results) -> None:
    """Increment retrieved_count for results not tracked by HybridRetriever."""
    from genesis.qdrant import collections as qdrant_ops

    for r in results:
        for coll in ("episodic_memory", "knowledge_base"):
            try:
                pts = qdrant.retrieve(coll, ids=[r.memory_id], with_payload=True)
                if pts:
                    old = (pts[0].payload or {}).get("retrieved_count", 0)
                    qdrant_ops.update_payload(
                        qdrant, collection=coll, point_id=r.memory_id,
                        payload={"retrieved_count": old + 1},
                    )
                    break
            except Exception:
                pass


@mcp.tool()
async def memory_recall(
    query: str,
    source: str | None = None,
    limit: int = 10,
    min_activation: float = 0.0,
    compact: bool = False,
    wing: str | None = None,
    room: str | None = None,
    include_graph: bool = True,
    expand_query_terms: bool = True,
    mode: str = "auto",
    time_range: str | None = None,
    include_subsystem: bool | list[str] = False,
    only_subsystem: str | list[str] | None = None,
) -> list[dict]:
    """Hybrid search: Qdrant vectors + FTS5, RRF fusion, with optional graph enrichment.

    Args:
        source: 'episodic' | 'knowledge' | 'both' | None. When None
            (default), classify_intent() routes the query to the best
            pool: WHY/WHEN/WHERE/STATUS → episodic; WHAT/HOW/GENERAL
            → both. Pass an explicit value to force a specific pool.
        compact: If True, return lightweight previews only (memory_id, preview,
            score, wing, room, memory_class, source). Use memory_expand to
            fetch full content for specific IDs. Saves tokens and ~500ms.
        wing: Filter results to this structural domain (e.g., "infrastructure").
        room: Filter results to this topic within a wing.
        include_graph: If False, skip graph traversal (saves ~500ms per call).
        expand_query_terms: If True, expand the FTS5 query via tag co-occurrence
            analysis (~500ms first call, ~10ms cached). Broadens recall for
            ambiguous queries. Default on — catches poor query formulation.
            Note: does not apply to the drift_recall fallback path (if wired).
        mode: Retrieval mode. "auto" = standard + drift fallback (default).
            "standard" = hybrid only, no drift fallback. "drift" = skip
            standard recall, use 3-phase drift retrieval directly. Drift
            mode ignores wing/room filters (discovers clusters dynamically).
        time_range: Explicit date range filter as "YYYY-MM-DD/YYYY-MM-DD".
            Queries the SVO event calendar and boosts temporally matching
            memories in RRF fusion. Automatic temporal detection also runs
            on queries with temporal language (e.g., "what happened last week").
        include_subsystem: Subsystem-filter additive mode. ``False`` (default)
            excludes automated-subsystem writes (ego corrections, triage
            signals, reflection observations). ``True`` returns everything.
            A list (e.g. ``["ego"]``) augments user content with the named
            subsystems. Mutually exclusive with ``only_subsystem``.
        only_subsystem: Subsystem-filter replace mode. Return ONLY rows
            tagged with the named subsystem(s); user content excluded.
            Used by ego's own self-recall path.
    """
    import time as _time

    _t0 = _time.monotonic()
    memory_mod = _memory_mod()
    memory_mod._require_init()
    assert memory_mod._retriever is not None and memory_mod._db is not None

    # Resolve source=None to the intent-recommended pool here, before
    # dispatching to retriever or drift_recall. Both consumers expect a
    # concrete string; HybridRetriever.recall accepts None but the drift
    # path uses dict.get(source, [...]) which would silently return the
    # default branch for None.
    if source is None:
        from genesis.memory.intent import classify_intent
        source = classify_intent(query).recommended_source

    pipeline_used = mode  # track which pipeline actually ran

    # Explicit time_range: query event calendar and merge IDs into results
    event_boost_ids: set[str] = set()
    if time_range:
        try:
            parts = time_range.split("/", 1)
            if len(parts) == 2:
                from genesis.db.crud import memory_events
                event_boost_ids = set(
                    await memory_events.get_memory_ids_in_range(
                        memory_mod._db, parts[0], parts[1], limit=limit * 3,
                    )
                )
        except Exception:
            logger.warning("time_range event query failed", exc_info=True)

    if mode == "drift":
        # Direct DRIFT invocation — skip standard recall entirely
        from genesis.memory.drift import drift_recall

        results = await drift_recall(
            query,
            db=memory_mod._db,
            qdrant_client=memory_mod._qdrant,
            embedding_provider=memory_mod._retriever._embeddings,
            source=source,
            limit=limit,
            min_activation=min_activation,
            include_subsystem=include_subsystem,
            only_subsystem=only_subsystem,
        )
        _increment_retrieved(memory_mod._qdrant, results)
    else:
        results = await memory_mod._retriever.recall(
            query, source=source, limit=limit, min_activation=min_activation,
            wing=wing, room=room, expand_query_terms=expand_query_terms,
            include_subsystem=include_subsystem,
            only_subsystem=only_subsystem,
        )
        pipeline_used = "standard"

        # Drift fallback (mode="auto" only): when standard recall returns
        # sparse results, try drift retrieval automatically.
        if (
            mode == "auto"
            and len(results) < min(3, limit)
            and limit >= 3
            and not wing
            and not room
        ):
            try:
                from genesis.memory.drift import drift_recall

                drift_results = await drift_recall(
                    query,
                    db=memory_mod._db,
                    qdrant_client=memory_mod._qdrant,
                    embedding_provider=memory_mod._retriever._embeddings,
                    source=source,
                    limit=limit,
                    min_activation=min_activation,
                    include_subsystem=include_subsystem,
                    only_subsystem=only_subsystem,
                )
                if len(drift_results) > len(results):
                    logger.info(
                        "drift_recall fallback: standard=%d → drift=%d results"
                        " (query=%r)",
                        len(results), len(drift_results), query[:80],
                    )
                    results = drift_results
                    _increment_retrieved(memory_mod._qdrant, drift_results)
                    pipeline_used = "auto_drift"
            except Exception:
                logger.warning("drift_recall fallback failed", exc_info=True)

    # Boost event-calendar matches from explicit time_range
    if event_boost_ids:
        from genesis.db.crud import memory as memory_crud
        from genesis.memory.types import RetrievalResult

        result_ids = {r.memory_id for r in results}
        missing = event_boost_ids - result_ids
        for mid in list(missing)[:limit]:
            try:
                row = await memory_crud.get_by_id(memory_mod._db, mid)
                if row:
                    results.append(RetrievalResult(
                        memory_id=mid,
                        content=row.get("content", ""),
                        source=row.get("source_type", ""),
                        memory_type=row.get("collection", ""),
                        score=0.01,
                        vector_rank=None,
                        fts_rank=None,
                        activation_score=0.0,
                        payload=row,
                        source_pipeline="event_calendar",
                    ))
            except Exception:
                logger.warning(
                    "Failed to fetch event-calendar memory %s", mid,
                    exc_info=True,
                )

    # MCP-layer instrumentation: emit with mode and pipeline attribution
    try:
        from genesis.eval.j9_hooks import emit_recall_fired
        await emit_recall_fired(
            memory_mod._db,
            query=query,
            result_count=len(results),
            top_scores=[r.score for r in results[:5]],
            memory_ids=[r.memory_id for r in results[:10]],
            latency_ms=(_time.monotonic() - _t0) * 1000,
            source=source,
            mode=mode,
            pipeline_used=pipeline_used,
        )
    except Exception:
        pass  # instrumentation must never break recall

    if compact:
        return [
            {
                "memory_id": r.memory_id,
                "preview": r.content[:150].replace("\n", " "),
                "score": round(r.score, 3),
                "activation": round(r.activation_score, 3),
                "memory_class": r.memory_class,
                "wing": r.payload.get("wing", ""),
                "room": r.payload.get("room", ""),
                "source": r.source,
                "source_pipeline": r.source_pipeline or "",
            }
            for r in results
        ]

    enriched = []
    graph_budget_ms = 500.0
    graph_elapsed_ms = 0.0
    for r in results:
        d = asdict(r)
        if include_graph and graph_elapsed_ms < graph_budget_ms:
            try:
                traversal = await graph_traverse(
                    memory_mod._db, r.memory_id, max_depth=2, min_strength=0.3,
                )
                graph_elapsed_ms += traversal.query_ms
                if traversal.nodes:
                    d["graph_neighbors"] = [
                        {
                            "memory_id": n.memory_id,
                            "link_type": n.link_type,
                            "depth": n.depth,
                            "strength": n.strength,
                        }
                        for n in traversal.nodes[:5]
                    ]
            except Exception:
                logger.warning(
                    "Graph enrichment failed for %s", r.memory_id, exc_info=True,
                )
        enriched.append(d)
    return enriched


@mcp.tool()
async def memory_expand(
    memory_ids: list[str],
) -> list[dict]:
    """Fetch full content + graph neighbors for specific memory IDs.

    Use after a compact memory_recall to selectively expand interesting results.
    Returns full RetrievalResult data with graph enrichment for each ID found.
    """
    memory_mod = _memory_mod()
    memory_mod._require_init()
    assert memory_mod._qdrant is not None and memory_mod._db is not None

    # Batch retrieve all IDs in a single Qdrant call
    try:
        points = memory_mod._qdrant.retrieve(
            collection_name="episodic_memory",
            ids=memory_ids,
            with_payload=True,
        )
    except Exception:
        logger.warning("Qdrant batch retrieve failed", exc_info=True)
        return []

    found_ids = {str(p.id) for p in points}
    not_found = [mid for mid in memory_ids if mid not in found_ids]

    results = []
    for point in points:
        mid = str(point.id)
        payload = point.payload or {}

        d = {
            "memory_id": mid,
            "content": payload.get("content", ""),
            "source": payload.get("source", ""),
            "memory_type": payload.get("memory_type", "episodic"),
            "memory_class": payload.get("memory_class", "fact"),
            "wing": payload.get("wing", ""),
            "room": payload.get("room", ""),
            "confidence": payload.get("confidence"),
            "tags": payload.get("tags", []),
            "source_pipeline": payload.get("source_pipeline", ""),
            "source_session_id": payload.get("source_session_id"),
            "created_at": payload.get("created_at"),
        }

        # Graph enrichment
        try:
            traversal = await graph_traverse(
                memory_mod._db, mid, max_depth=2, min_strength=0.3,
            )
            if traversal.nodes:
                d["graph_neighbors"] = [
                    {
                        "memory_id": n.memory_id,
                        "link_type": n.link_type,
                        "depth": n.depth,
                        "strength": n.strength,
                    }
                    for n in traversal.nodes[:5]
                ]
        except Exception:
            logger.warning("Graph enrichment failed for %s", mid, exc_info=True)

        results.append(d)

    if not_found:
        results.append({"not_found": not_found})

    return results


@mcp.tool()
async def memory_store(
    content: str,
    source: str,
    memory_type: str = "episodic",
    tags: list[str] | None = None,
    confidence: float = 0.5,
    memory_class: str | None = None,
    wing: str | None = None,
    room: str | None = None,
    collection: str | None = None,
) -> str:
    """Store memory with source metadata and type tag. Returns memory_id.

    Args:
        memory_class: Optional classification — "rule", "fact", or "reference".
            Auto-classified from content if not provided.
        wing: Structural domain (auto-classified if not provided).
        room: Topic within the wing (auto-classified if not provided).
        collection: Explicit Qdrant collection override. Bypasses the default
            collection routing when provided (e.g. "knowledge_base").
    """
    memory_mod = _memory_mod()
    memory_mod._require_init()
    assert memory_mod._store is not None
    return await memory_mod._store.store(
        content, source, memory_type=memory_type, tags=tags, confidence=confidence,
        memory_class=memory_class, source_pipeline="conversation",
        wing=wing, room=room, collection=collection,
    )


@mcp.tool()
async def memory_extract(
    extractions: list[dict],
) -> list[str]:
    """Store fact/decision/entity extractions. Returns list of IDs."""
    memory_mod = _memory_mod()
    memory_mod._require_init()
    assert memory_mod._store is not None
    ids: list[str] = []
    for item in extractions:
        mid = await memory_mod._store.store(
            content=item["content"],
            source=item.get("source", "extraction"),
            memory_type=item.get("type", "fact"),
            tags=item.get("tags"),
            confidence=item.get("confidence", 0.7),
            source_pipeline="harvest",
        )
        ids.append(mid)
    return ids


@mcp.tool()
async def memory_proactive(
    current_message: str,
    limit: int = 5,
) -> list[dict]:
    """Cross-session context injection for prompts."""
    memory_mod = _memory_mod()
    memory_mod._require_init()
    assert memory_mod._retriever is not None
    # min_activation=0.0: use activation as a ranking signal, not a filter gate.
    # With confidence=0.5 (96% of memories) and retrieved_count=0 (80%),
    # even day-old memories fail a 0.3 threshold. Let RRF fusion rank instead.
    results = await memory_mod._retriever.recall(current_message, limit=limit * 2, min_activation=0.0)
    filtered = [
        r for r in results
        if "memory_operation" not in (r.payload.get("tags") or [])
    ][:limit]
    return [asdict(r) for r in filtered]


@mcp.tool()
async def memory_core_facts(
    limit: int = 10,
) -> list[dict]:
    """High-confidence memories for system prompt injection.

    Queries the memory store (Qdrant) for memories with confidence >= 0.7,
    ranked by activation score. Returns compact summaries.
    """
    memory_mod = _memory_mod()
    memory_mod._require_init()
    assert memory_mod._qdrant is not None and memory_mod._db is not None

    # Query high-confidence memories across all wings
    # Use a broad query to get candidates, then re-rank by activation
    try:
        from qdrant_client.models import FieldCondition, Filter, Range

        points = memory_mod._qdrant.scroll(
            collection_name="episodic_memory",
            scroll_filter=Filter(must=[
                FieldCondition(key="confidence", range=Range(gte=0.7)),
            ]),
            limit=limit * 3,
            with_payload=True,
        )[0]  # scroll returns (points, next_offset)
    except Exception:
        logger.warning("Qdrant scroll for core_facts failed", exc_info=True)
        return []

    now_str = datetime.now(UTC).isoformat()
    scored: list[tuple[dict, float]] = []
    for point in points:
        payload = point.payload or {}
        mid = str(point.id)
        link_count = await memory_mod.memory_links.count_links(memory_mod._db, mid)
        act = compute_activation(
            confidence=payload.get("confidence", 0.7),
            created_at=payload.get("created_at", now_str),
            retrieved_count=payload.get("retrieved_count", 0),
            link_count=link_count,
            source=payload.get("source", ""),
            now=now_str,
        )
        scored.append((
            {
                "memory_id": mid,
                "content": payload.get("content", ""),
                "source": payload.get("source", ""),
                "memory_class": payload.get("memory_class", "fact"),
                "wing": payload.get("wing", ""),
                "room": payload.get("room", ""),
                "confidence": payload.get("confidence"),
                "activation_score": round(act.final_score, 3),
            },
            act.final_score,
        ))

    scored.sort(key=lambda x: x[1], reverse=True)
    top = scored[:limit]

    # Track retrieval so activation scores reflect actual usage
    if top:
        try:
            for item, _ in top:
                mid = item["memory_id"]
                pts = memory_mod._qdrant.retrieve(
                    collection_name="episodic_memory", ids=[mid], with_payload=True,
                )
                if pts:
                    old_count = (pts[0].payload or {}).get("retrieved_count", 0)
                    memory_mod._qdrant.set_payload(
                        collection_name="episodic_memory",
                        payload={"retrieved_count": old_count + 1},
                        points=[mid],
                    )
        except Exception:
            logger.debug("Failed to update retrieved_count for core_facts", exc_info=True)

    return [item for item, _ in top]


@mcp.tool()
async def memory_stats() -> dict:
    """Health, capacity, and structural metrics for the memory system."""
    memory_mod = _memory_mod()
    memory_mod._require_init()
    assert memory_mod._db is not None
    assert memory_mod._qdrant is not None

    episodic_info: dict | None = None
    knowledge_info: dict | None = None
    try:
        episodic_info = memory_mod.get_collection_info(memory_mod._qdrant, "episodic_memory")
    except Exception:
        logger.warning("Failed to query episodic_memory collection", exc_info=True)
    try:
        knowledge_info = memory_mod.get_collection_info(memory_mod._qdrant, "knowledge_base")
    except Exception:
        logger.warning("Failed to query knowledge_base collection", exc_info=True)

    pending_deltas = await memory_mod.observations.query(
        memory_mod._db, type="user_model_delta", resolved=False, limit=100000
    )

    total_links_cursor = await memory_mod._db.execute("SELECT COUNT(*) FROM memory_links")
    total_links_row = await total_links_cursor.fetchone()
    total_links = total_links_row[0] if total_links_row else 0

    # Structural data from memory_health snapshot queries
    wings = []
    classes = []
    extraction = {}
    code_index = {}
    ek_info = {}
    try:
        from genesis.observability.snapshots.memory_health import (
            _class_distribution,
            _code_index_stats,
            _essential_knowledge_stats,
            _extraction_coverage,
            _wing_distribution,
        )
        wings = await _wing_distribution(memory_mod._db)
        classes = await _class_distribution(memory_mod._db)
        extraction = await _extraction_coverage(memory_mod._db)
        code_index = await _code_index_stats(memory_mod._db)
        ek_info = _essential_knowledge_stats()
    except Exception:
        logger.debug("Structural stats unavailable", exc_info=True)

    return {
        "episodic_count": episodic_info.get("points_count", 0) if episodic_info else None,
        "knowledge_count": knowledge_info.get("points_count", 0) if knowledge_info else None,
        "pending_deltas": len(pending_deltas),
        "total_links": total_links,
        "wings": wings,
        "classes": classes,
        "extraction": extraction,
        "code_index": code_index,
        "essential_knowledge": ek_info,
    }


@mcp.tool()
async def memory_synthesize(
    content: str,
    source_memory_ids: list[str] | None = None,
    tags: list[str] | None = None,
    wing: str | None = None,
    room: str | None = None,
) -> str:
    """Store a synthesis — a conclusion derived from multiple recalled memories.

    Use this when you've combined information from multiple memories into a new
    insight worth preserving. The synthesis is stored with higher confidence
    (validated by use) and linked back to source memories.

    Args:
        content: The synthesized knowledge.
        source_memory_ids: IDs of memories that contributed to this synthesis.
        tags: Additional tags for the synthesis.
        wing: Structural domain (auto-classified if not provided).
        room: Topic within the wing (auto-classified if not provided).

    Returns:
        The memory_id of the stored synthesis.
    """
    memory_mod = _memory_mod()
    memory_mod._require_init()
    assert memory_mod._store is not None

    resolved_tags = list(tags or [])
    if "synthesis" not in resolved_tags:
        resolved_tags.append("synthesis")

    memory_id = await memory_mod._store.store(
        content,
        source="synthesis",
        memory_type="episodic",
        tags=resolved_tags,
        confidence=0.8,  # Higher confidence — validated by cross-memory derivation
        source_pipeline="synthesis",
        wing=wing,
        room=room,
    )

    # Create links back to source memories
    if source_memory_ids and memory_mod._store.linker:
        for source_id in source_memory_ids:
            try:
                await memory_mod._store.linker.create_typed_links(
                    memory_id,
                    [{"target": source_id, "type": "extends"}],
                )
            except Exception:
                logger.warning(
                    "Failed to link synthesis %s to source %s",
                    memory_id, source_id, exc_info=True,
                )

    return memory_id
