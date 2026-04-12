"""Core memory tools: recall, store, extract, proactive, core_facts, stats."""

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


@mcp.tool()
async def memory_recall(
    query: str,
    source: str = "both",
    limit: int = 10,
    min_activation: float = 0.0,
) -> list[dict]:
    """Hybrid search: Qdrant vectors + FTS5, RRF fusion, with graph enrichment."""
    memory_mod = _memory_mod()
    memory_mod._require_init()
    assert memory_mod._retriever is not None and memory_mod._db is not None
    results = await memory_mod._retriever.recall(
        query, source=source, limit=limit, min_activation=min_activation
    )
    enriched = []
    graph_budget_ms = 500.0
    graph_elapsed_ms = 0.0
    for r in results:
        d = asdict(r)
        if graph_elapsed_ms < graph_budget_ms:
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
async def memory_store(
    content: str,
    source: str,
    memory_type: str = "episodic",
    tags: list[str] | None = None,
    confidence: float = 0.5,
    memory_class: str | None = None,
) -> str:
    """Store memory with source metadata and type tag. Returns memory_id.

    Args:
        memory_class: Optional classification — "rule", "fact", or "reference".
            Auto-classified from content if not provided.
    """
    memory_mod = _memory_mod()
    memory_mod._require_init()
    assert memory_mod._store is not None
    return await memory_mod._store.store(
        content, source, memory_type=memory_type, tags=tags, confidence=confidence,
        memory_class=memory_class, source_pipeline="conversation",
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
    """High-confidence items for system prompt injection."""
    memory_mod = _memory_mod()
    memory_mod._require_init()
    assert memory_mod._db is not None
    candidate_limit = limit * 3
    facts = await memory_mod.observations.query(
        memory_mod._db, type="learning", resolved=False, limit=candidate_limit
    )
    decisions = await memory_mod.observations.query(
        memory_mod._db, type="reflection_observation", resolved=False, limit=candidate_limit
    )

    seen: set[str] = set()
    merged: list[dict] = []
    for obs in facts + decisions:
        oid = obs["id"]
        if oid not in seen:
            seen.add(oid)
            merged.append(obs)

    now_str = datetime.now(UTC).isoformat()
    scored: list[tuple[dict, float]] = []
    for obs in merged:
        link_count = await memory_mod.memory_links.count_links(memory_mod._db, obs["id"])
        act = compute_activation(
            confidence=0.8,
            created_at=obs.get("created_at", now_str),
            retrieved_count=obs.get("retrieved_count", 0),
            link_count=link_count,
            source=obs.get("source", obs.get("type", "")),
            now=now_str,
        )
        scored.append((obs, act.final_score))

    scored.sort(key=lambda x: x[1], reverse=True)
    top_items = scored[:limit]

    top_ids = [item["id"] for item, _ in top_items]
    if top_ids:
        try:
            await memory_mod.observations.increment_retrieved_batch(memory_mod._db, top_ids)
        except Exception:
            logger.warning("Failed to track observation retrieval in core_facts", exc_info=True)

    return [item for item, _ in top_items]


@mcp.tool()
async def memory_stats() -> dict:
    """Health and capacity metrics."""
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

    return {
        "episodic_count": episodic_info.get("points_count", 0) if episodic_info else None,
        "knowledge_count": knowledge_info.get("points_count", 0) if knowledge_info else None,
        "pending_deltas": len(pending_deltas),
        "total_links": total_links,
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
