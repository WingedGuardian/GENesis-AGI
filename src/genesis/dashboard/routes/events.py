"""Paginated events API and event detail routes."""

from __future__ import annotations

import contextlib
import json
from pathlib import Path

import aiosqlite
from flask import jsonify, request, send_from_directory

from genesis.dashboard._blueprint import _async_route, blueprint, logger

TEMPLATE_DIR = Path(__file__).parent.parent / "templates"


@blueprint.route("/genesis/logs")
def logs_page():
    """Serve the Master Event Log page."""
    return send_from_directory(str(TEMPLATE_DIR), "genesis_logs.html")


@blueprint.route("/genesis/errors")
def errors_page():
    """Serve the Master Error Log page."""
    return send_from_directory(str(TEMPLATE_DIR), "genesis_errors.html")


@blueprint.route("/api/genesis/events")
@_async_route
async def events_paginated():
    """Cursor-based paginated event log with rich filtering."""
    from genesis.db.crud import events as events_crud
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.db is None:
        return jsonify({"events": [], "has_more": False, "total_matching": 0, "next_cursor": None})

    page_size = min(request.args.get("page_size", 100, type=int), 500)
    cursor_ts = request.args.get("cursor_ts")
    cursor_id = request.args.get("cursor_id")
    severities_str = request.args.get("severities")
    subsystems_str = request.args.get("subsystems")
    event_types_str = request.args.get("event_types")
    search = request.args.get("search")
    since = request.args.get("since")
    until = request.args.get("until")

    subsystems = [s.strip() for s in subsystems_str.split(",") if s.strip()] if subsystems_str else None
    event_types = [e.strip() for e in event_types_str.split(",") if e.strip()] if event_types_str else None

    min_severity = None
    if severities_str:
        parts = [s.strip() for s in severities_str.split(",") if s.strip()]
        if len(parts) == 1:
            min_severity = parts[0]
        else:
            from genesis.db.crud.events import _SEVERITY_ORDER
            for sev in _SEVERITY_ORDER:
                if sev in parts:
                    min_severity = sev
                    break

    filter_kwargs = dict(
        min_severity=min_severity,
        subsystems=subsystems,
        event_types=event_types,
        search=search,
        since=since,
        until=until,
    )

    try:
        events, has_more = await events_crud.query_paginated(
            rt.db,
            cursor_ts=cursor_ts,
            cursor_id=cursor_id,
            page_size=page_size,
            **filter_kwargs,
        )
    except Exception:
        logger.warning("Failed to query paginated events", exc_info=True)
        return jsonify({"events": [], "has_more": False, "total_matching": 0, "next_cursor": None})

    total = 0
    if not cursor_ts:
        try:
            total = await events_crud.count_filtered(rt.db, **filter_kwargs)
        except Exception:
            logger.warning("Failed to count filtered events", exc_info=True)

    next_cursor = None
    if has_more and events:
        last = events[-1]
        next_cursor = {"ts": last["timestamp"], "id": last["id"]}

    return jsonify({
        "events": events,
        "has_more": has_more,
        "total_matching": total,
        "next_cursor": next_cursor,
    })


@blueprint.route("/api/genesis/events/<event_id>")
@_async_route
async def event_detail(event_id: str):
    """Return full details for a single event."""
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.db is None:
        return jsonify(None), 404

    rt.db.row_factory = aiosqlite.Row
    cursor = await rt.db.execute("SELECT * FROM events WHERE id = ?", (event_id,))
    row = await cursor.fetchone()
    if not row:
        return jsonify(None), 404
    d = dict(row)
    if d.get("details"):
        with contextlib.suppress(json.JSONDecodeError, TypeError):
            d["details"] = json.loads(d["details"])
    return jsonify(d)
