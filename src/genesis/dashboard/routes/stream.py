"""Real-time event stream via optimized polling.

SSE is blocked by AZ's WSGIMiddleware (buffers entire response before
forwarding).  This module provides a lightweight cursor-based polling
endpoint instead.  Dashboard JS uses ``EventSourceOrPoll`` which tries
``EventSource`` first (for Phase 2 standalone) and falls back to this.
"""

from __future__ import annotations

import logging

from flask import jsonify, request

from genesis.dashboard._blueprint import _async_route, blueprint

logger = logging.getLogger(__name__)


@blueprint.route("/api/genesis/stream/poll")
@_async_route
async def stream_poll():
    """Return events newer than *cursor* (ISO timestamp).

    Query params:
        cursor   – ISO timestamp; only events after this are returned
        limit    – max events (default 50, capped at 200)
        severity – exact severity filter (debug/info/warning/error)
        subsystem – subsystem filter

    Returns ``{events: [...], cursor: "<newest-timestamp>", has_more: bool}``.
    Designed for 3-5 s polling intervals.
    """
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.db is None:
        return jsonify({"events": [], "cursor": None, "has_more": False})

    cursor = request.args.get("cursor")
    limit = min(request.args.get("limit", 50, type=int), 200)
    severity = request.args.get("severity")
    subsystem = request.args.get("subsystem")

    from genesis.db.crud import events as events_crud

    try:
        rows = await events_crud.query(
            rt.db,
            severity=severity,
            subsystem=subsystem,
            since=cursor,
            limit=limit,
        )
    except Exception:
        logger.error("Event stream poll query failed", exc_info=True)
        return jsonify({"events": [], "cursor": cursor, "has_more": False})

    # Newest event timestamp becomes the next cursor
    new_cursor = rows[0]["timestamp"] if rows else cursor
    has_more = len(rows) >= limit

    return jsonify({
        "events": rows,
        "cursor": new_cursor,
        "has_more": has_more,
    })
