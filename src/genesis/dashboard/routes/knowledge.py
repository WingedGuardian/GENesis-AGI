"""Knowledge base browser routes — search, recent, detail, delete, stats."""

from __future__ import annotations

import logging

from flask import jsonify, request

from genesis.dashboard._blueprint import _async_route, blueprint

logger = logging.getLogger(__name__)


@blueprint.route("/api/genesis/knowledge/search")
@_async_route
async def knowledge_search():
    """FTS5 search with optional domain/project/tier filters.

    Query params:
        q       – search query (required)
        domain  – filter by domain
        project – filter by project_type
        limit   – max results (default 20, capped at 100)
    """
    from genesis.db.crud import knowledge
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.db is None:
        return jsonify({"error": "Not bootstrapped"}), 503

    query = request.args.get("q", "").strip()
    if not query:
        return jsonify({"error": "q parameter required"}), 400

    domain = request.args.get("domain") or None
    project = request.args.get("project") or None
    limit = max(1, min(request.args.get("limit", 20, type=int), 100))

    try:
        results = await knowledge.search_fts(
            rt.db, query, project=project, domain=domain, limit=limit,
        )
        return jsonify({
            "results": results,
            "query": query,
            "count": len(results),
        })
    except Exception:
        logger.exception("Knowledge search failed")
        return jsonify({"error": "Search failed"}), 500


@blueprint.route("/api/genesis/knowledge/recent")
@_async_route
async def knowledge_recent():
    """List recent knowledge units by ingestion date.

    Query params:
        limit  – max results (default 50, capped at 200)
        offset – pagination offset
    """
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.db is None:
        return jsonify({"error": "Not bootstrapped"}), 503

    limit = max(1, min(request.args.get("limit", 50, type=int), 200))
    offset = max(0, request.args.get("offset", 0, type=int))

    try:
        cursor = await rt.db.execute(
            "SELECT * FROM knowledge_units ORDER BY ingested_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        )
        rows = await cursor.fetchall()
        columns = [desc[0] for desc in cursor.description]

        cursor_total = await rt.db.execute("SELECT COUNT(*) FROM knowledge_units")
        total = (await cursor_total.fetchone())[0]

        return jsonify({
            "units": [dict(zip(columns, row, strict=False)) for row in rows],
            "total": total,
            "offset": offset,
        })
    except Exception:
        logger.exception("Knowledge recent failed")
        return jsonify({"error": "Failed to fetch recent units"}), 500


@blueprint.route("/api/genesis/knowledge/<unit_id>")
@_async_route
async def knowledge_detail(unit_id: str):
    """Full detail for a single knowledge unit."""
    from genesis.db.crud import knowledge
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.db is None:
        return jsonify({"error": "Not bootstrapped"}), 503

    try:
        unit = await knowledge.get(rt.db, unit_id)
        if unit is None:
            return jsonify({"error": "Unit not found"}), 404
        return jsonify({"unit": unit})
    except Exception:
        logger.exception("Knowledge detail failed")
        return jsonify({"error": "Failed to fetch unit"}), 500


@blueprint.route("/api/genesis/knowledge/<unit_id>", methods=["DELETE"])
@_async_route
async def knowledge_delete(unit_id: str):
    """Delete a knowledge unit from SQLite + Qdrant."""
    from genesis.db.crud import knowledge
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.db is None:
        return jsonify({"error": "Not bootstrapped"}), 503

    try:
        # Get Qdrant ID before deleting from SQLite
        unit = await knowledge.get(rt.db, unit_id)
        qdrant_id = unit.get("qdrant_id") if unit else None

        deleted = await knowledge.delete(rt.db, unit_id)
        if not deleted:
            return jsonify({"error": "Unit not found"}), 404

        # Also delete from Qdrant if we have a reference
        qdrant_deleted = False
        if qdrant_id and rt.qdrant_client:
            try:
                from qdrant_client.models import PointIdsList

                rt.qdrant_client.delete(
                    collection_name="knowledge_base",
                    points_selector=PointIdsList(points=[qdrant_id]),
                )
                qdrant_deleted = True
            except Exception:
                logger.warning("Failed to delete Qdrant point %s", qdrant_id)

        return jsonify({
            "status": "ok",
            "sqlite_deleted": True,
            "qdrant_deleted": qdrant_deleted,
        })
    except Exception:
        logger.exception("Knowledge delete failed")
        return jsonify({"error": "Delete failed"}), 500


@blueprint.route("/api/genesis/knowledge/stats")
@_async_route
async def knowledge_stats():
    """Aggregate stats: total, by domain, by tier."""
    from genesis.db.crud import knowledge
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.db is None:
        return jsonify({"error": "Not bootstrapped"}), 503

    try:
        stats = await knowledge.stats(rt.db)

        qdrant_count = None
        if rt.qdrant_client:
            try:
                from genesis.qdrant.collections import get_collection_info

                info = get_collection_info(rt.qdrant_client, "knowledge_base")
                qdrant_count = info.get("points_count", 0) if info else None
            except Exception:
                pass

        return jsonify({
            **stats,
            "qdrant_vectors": qdrant_count,
        })
    except Exception:
        logger.exception("Knowledge stats failed")
        return jsonify({"error": "Failed to fetch stats"}), 500
