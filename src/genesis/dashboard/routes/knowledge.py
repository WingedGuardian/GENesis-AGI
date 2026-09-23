"""Knowledge base browser routes — search, recent, detail, delete, stats.

**These routes serve the knowledge base, NOT the reference store.** Both live in
``knowledge_units``; the reference store is the ``project_type='reference'``
partition and it holds stored credentials, network facts and account details in
each row's ``body``.

``references.py`` owns that partition and renders it behind value-free
summaries, kind masking and an explicit reveal step — and it already scopes
ITSELF to it, passing ``project_type=REFERENCE_PROJECT`` on every query and
refusing a non-reference row outright in ``references_detail``. The partition
was one-sided: nothing here refused the reference rows, so these routes were a
second, uncontrolled door onto the same values. ``_EXCLUDED_PROJECT`` closes
the other half, so each module serves its own partition and refuses the other's.

Stated limit, so this is not read as a general guarantee: the exclusion names
the reference store specifically. A FUTURE secret-bearing partition would not
be covered and must be added here — these routes select by ``*`` and will
return whatever columns a new store puts in a row.
"""

from __future__ import annotations

import logging

from flask import jsonify, request

from genesis.dashboard._blueprint import _async_route, blueprint
from genesis.memory.reference_ops import REFERENCE_PROJECT

logger = logging.getLogger(__name__)

# The partition these routes must never serve. Imported rather than spelled
# again so the two halves of the split cannot drift apart: `references.py`
# selects exactly this value, and everything here refuses it.
_EXCLUDED_PROJECT = REFERENCE_PROJECT


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

    # A caller-supplied `project` must not be able to SELECT the excluded
    # partition — `?project=reference` was the shortest path to the whole
    # reference store. Refusing beats silently returning nothing: the caller
    # asked for a store this endpoint does not serve, and `references.py` is
    # where it lives.
    if project == _EXCLUDED_PROJECT:
        return jsonify({
            "error": f"project '{_EXCLUDED_PROJECT}' is not served here",
            "use": "/api/genesis/references/search",
        }), 400

    try:
        results = await knowledge.search_fts(
            rt.db,
            query,
            project=project,
            exclude_project=_EXCLUDED_PROJECT,
            domain=domain,
            limit=limit,
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
        # NULL-SAFE exclusion. `project_type != ?` evaluates to NULL — and so
        # is not TRUE — for a row that declares no project_type, which would
        # drop every untyped row from the listing. The clause must remove one
        # partition, not everything that failed to name one.
        _not_excluded = "WHERE (project_type IS NULL OR project_type != ?)"
        cursor = await rt.db.execute(
            f"SELECT * FROM knowledge_units {_not_excluded}"
            " ORDER BY ingested_at DESC LIMIT ? OFFSET ?",
            (_EXCLUDED_PROJECT, limit, offset),
        )
        rows = await cursor.fetchall()
        columns = [desc[0] for desc in cursor.description]

        # The total must count the SAME set the rows come from, or paging walks
        # off the end of a list shorter than it was told to expect.
        cursor_total = await rt.db.execute(
            f"SELECT COUNT(*) FROM knowledge_units {_not_excluded}",
            (_EXCLUDED_PROJECT,),
        )
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
        # The mirror of ``references_detail``, which refuses a row that is NOT
        # in the reference partition. 404 rather than 403: whether a given id
        # exists in the other store is itself not this endpoint's to disclose.
        if unit is None or unit.get("project_type") == _EXCLUDED_PROJECT:
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
        # Refused for the same reason the read is: this endpoint does not serve
        # the reference partition, and that cuts both ways. Without it the
        # knowledge browser can DESTROY a stored credential it is not allowed
        # to show — and on a passwordless install, unauthenticated.
        if unit is not None and unit.get("project_type") == _EXCLUDED_PROJECT:
            return jsonify({"error": "Unit not found"}), 404
        qdrant_id = unit.get("qdrant_id") if unit else None

        deleted = await knowledge.delete(rt.db, unit_id)
        if not deleted:
            return jsonify({"error": "Unit not found"}), 404

        # Also delete from Qdrant. The client lives on the memory store, not on
        # the runtime directly — rt.qdrant_client never existed (bug since #83,
        # sibling of the dream_cycle rt.qdrant→store.qdrant_client fix in #385).
        qdrant_deleted = False
        store = getattr(rt, "memory_store", None)
        if qdrant_id and store is not None:
            try:
                from qdrant_client.models import PointIdsList

                store.qdrant_client.delete(
                    collection_name="knowledge_base",
                    points_selector=PointIdsList(points=[qdrant_id]),
                )
                qdrant_deleted = True
            except Exception:
                logger.warning("Failed to delete Qdrant point %s", qdrant_id)

        # Drop the unit from the ingestion manifest so a fully-deleted source is
        # not permanently blocked from re-ingest by the source-identity gate.
        # Best-effort: the unit is already physically gone; manifest bookkeeping
        # must never fail the delete.
        manifest_removed = False
        try:
            from genesis.knowledge.manifest import ManifestManager

            manifest_removed = ManifestManager().remove_unit(unit_id)
        except Exception:
            logger.warning("Failed to update manifest after deleting unit %s", unit_id)

        return jsonify({
            "status": "ok",
            "sqlite_deleted": True,
            "qdrant_deleted": qdrant_deleted,
            "manifest_removed": manifest_removed,
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
        # Scoped to the same partition the listings serve, so the count the
        # Knowledge tab shows matches what browsing it can actually reach.
        stats = await knowledge.stats(rt.db, exclude_project=_EXCLUDED_PROJECT)

        qdrant_count = None
        try:
            from qdrant_client import QdrantClient

            from genesis.env import qdrant_url
            from genesis.qdrant.collections import get_collection_info

            qdrant = QdrantClient(url=qdrant_url(), timeout=3)
            info = get_collection_info(qdrant, "knowledge_base")
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
