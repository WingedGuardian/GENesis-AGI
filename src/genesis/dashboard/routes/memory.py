"""Memory browser routes — search, recent, detail, delete, stats."""

from __future__ import annotations

import logging

from flask import jsonify, request

from genesis.dashboard._blueprint import _async_route, blueprint

logger = logging.getLogger(__name__)


@blueprint.route("/api/genesis/memory/search")
@_async_route
async def memory_search():
    """Semantic + FTS5 hybrid search.

    Query params:
        q     – search query (required)
        limit – max results (default 20, capped at 100)
    """
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.db is None:
        return jsonify({"error": "Not bootstrapped"}), 503

    query = request.args.get("q", "").strip()
    if not query:
        return jsonify({"error": "q parameter required"}), 400

    limit = max(1, min(request.args.get("limit", 20, type=int), 100))

    try:
        # Use runtime's pre-built retriever (requires embedding provider + Qdrant)
        if rt.hybrid_retriever is None:
            return jsonify({"error": "Memory retriever not initialized"}), 503

        results = await rt.hybrid_retriever.recall(query=query, limit=limit)

        items = []
        for r in results:
            items.append({
                "memory_id": r.memory_id,
                "content": r.content[:500] if r.content else "",
                "source": r.source,
                "memory_type": r.memory_type,
                "score": round(r.score, 4) if r.score else None,
                "vector_rank": r.vector_rank,
                "fts_rank": r.fts_rank,
                "activation_score": round(r.activation_score, 4) if r.activation_score else None,
                "source_session_id": r.source_session_id,
                "source_pipeline": r.source_pipeline,
            })

        return jsonify({"results": items, "query": query, "count": len(items)})
    except Exception:
        logger.error("Memory search failed", exc_info=True)
        return jsonify({"error": "Search failed"}), 500


@blueprint.route("/api/genesis/memory/recent")
@_async_route
async def memory_recent():
    """List recent memories by timestamp.

    Query params:
        limit      – max results (default 50, capped at 200)
        offset     – pagination offset (default 0)
        collection – filter by collection (optional)
    """
    from genesis.db.crud import memory as memory_crud
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.db is None:
        return jsonify({"error": "Not bootstrapped"}), 503

    limit = max(1, min(request.args.get("limit", 50, type=int), 200))
    offset = max(0, request.args.get("offset", 0, type=int))
    collection = request.args.get("collection")

    try:
        items = await memory_crud.list_recent(
            rt.db, limit=limit, offset=offset, collection=collection,
        )
        total = await memory_crud.count(rt.db, collection=collection)
        return jsonify({"memories": items, "total": total, "offset": offset})
    except Exception:
        logger.error("Memory recent query failed", exc_info=True)
        return jsonify({"error": "Query failed"}), 500


@blueprint.route("/api/genesis/memory/<memory_id>")
@_async_route
async def memory_detail(memory_id: str):
    """Get full detail for a single memory."""
    from genesis.db.crud import memory as memory_crud
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.db is None:
        return jsonify({"error": "Not bootstrapped"}), 503

    try:
        item = await memory_crud.get_by_id(rt.db, memory_id)
        if not item:
            return jsonify({"error": "Memory not found"}), 404

        # Enrich with link count
        from genesis.db.crud import memory_links

        link_count = await memory_links.count_links(rt.db, memory_id)
        item["link_count"] = link_count

        return jsonify({"memory": item})
    except Exception:
        logger.error("Memory detail failed for %s", memory_id, exc_info=True)
        return jsonify({"error": "Query failed"}), 500


@blueprint.route("/api/genesis/memory/<memory_id>", methods=["DELETE"])
@_async_route
async def memory_delete(memory_id: str):
    """Coordinated delete from all memory layers."""
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.db is None:
        return jsonify({"error": "Not bootstrapped"}), 503

    try:
        if rt.memory_store is None:
            # Fallback: FTS5-only delete if MemoryStore not available
            from genesis.db.crud import memory as memory_crud

            deleted = await memory_crud.delete(rt.db, memory_id=memory_id)
            await memory_crud.delete_metadata(rt.db, memory_id=memory_id)
            return jsonify({"status": "partial", "fts5": deleted})

        results = await rt.memory_store.delete(memory_id)
        return jsonify({"status": "ok", "details": results})
    except Exception:
        logger.error("Memory delete failed for %s", memory_id, exc_info=True)
        return jsonify({"error": "Delete failed"}), 500


@blueprint.route("/api/genesis/memory/stats")
@_async_route
async def memory_stats():
    """Memory system statistics — counts by layer, embedding health."""
    from genesis.db.crud import memory as memory_crud
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.db is None:
        return jsonify({"error": "Not bootstrapped"}), 503

    stats = {}

    try:
        stats["total"] = await memory_crud.count(rt.db)
        stats["episodic"] = await memory_crud.count(rt.db, collection="episodic_memory")
        stats["knowledge"] = await memory_crud.count(rt.db, collection="knowledge_base")
    except Exception:
        logger.error("Memory count failed", exc_info=True)
        stats["total"] = None

    # Qdrant collection stats
    try:
        from genesis.qdrant.collections import get_client, get_collection_info

        client = get_client()
        for coll in ("episodic_memory", "knowledge_base"):
            try:
                info = get_collection_info(client, coll)
                stats[f"qdrant_{coll}"] = info.get("points_count", 0)
            except Exception:
                logger.error("Qdrant stats failed for %s", coll, exc_info=True)
                stats[f"qdrant_{coll}"] = None
    except Exception:
        stats["qdrant_episodic_memory"] = None
        stats["qdrant_knowledge_base"] = None

    # Pending embeddings count
    try:
        from genesis.db.crud import pending_embeddings

        stats["pending_embeddings"] = await pending_embeddings.count_pending(rt.db)
    except Exception:
        stats["pending_embeddings"] = None

    return jsonify(stats)
