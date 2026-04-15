"""Follow-up accountability routes for the dashboard.

Exposes follow-up list with status counts, visible alongside tasks.
"""

from __future__ import annotations

import logging

from flask import jsonify, request

from genesis.dashboard._blueprint import _async_route, blueprint

logger = logging.getLogger(__name__)


@blueprint.route("/api/genesis/follow-ups")
@_async_route
async def follow_up_list():
    """Return follow-ups with optional status filter.

    Query params:
        status – filter by status (default: all)
        limit – max results (default 30)
    """
    from genesis.db.crud import follow_ups
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.db is None:
        return jsonify({"follow_ups": [], "counts": {}})

    status_filter = request.args.get("status", "").strip() or None
    limit = min(request.args.get("limit", 30, type=int), 200)

    try:
        if status_filter:
            items = await follow_ups.get_by_status(rt.db, status_filter)
            items = items[:limit]
        else:
            items = await follow_ups.get_recent(rt.db, limit=limit)

        counts = await follow_ups.get_summary_counts(rt.db)
    except Exception:
        logger.error("Failed to list follow-ups", exc_info=True)
        return jsonify({"follow_ups": [], "counts": {}})

    return jsonify({
        "follow_ups": items,
        "counts": counts,
        "total": sum(counts.values()),
    })


@blueprint.route("/api/genesis/follow-ups/summary")
@_async_route
async def follow_up_summary():
    """Return just the counts by status for dashboard badges."""
    from genesis.db.crud import follow_ups
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.db is None:
        return jsonify({"counts": {}, "total": 0})

    try:
        counts = await follow_ups.get_summary_counts(rt.db)
        return jsonify({"counts": counts, "total": sum(counts.values())})
    except Exception:
        logger.error("Failed to get follow-up summary", exc_info=True)
        return jsonify({"counts": {}, "total": 0})
