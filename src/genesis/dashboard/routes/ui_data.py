"""Genesis UI data endpoints — sessions, memory, inbox, tasks.

These routes were formerly on the AZ-only genesis_ui blueprint.
Moving them to genesis_dashboard makes them available in standalone mode too.

The overlay JS (genesis-overlay.js) calls these via /api/genesis/ui/*
and continues to work unchanged — only the registering blueprint differs.
"""

from __future__ import annotations

import logging

import aiosqlite
from flask import jsonify, request

from genesis.dashboard._blueprint import _async_route, blueprint

logger = logging.getLogger(__name__)


@blueprint.route("/api/genesis/ui/sessions")
@_async_route
async def ui_sessions():
    """Return Genesis CC sessions shaped for the sidebar chats list."""
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.db is None:
        return jsonify([])

    limit = min(request.args.get("limit", 30, type=int), 100)

    rt.db.row_factory = aiosqlite.Row
    rows = await rt.db.execute_fetchall(
        "SELECT * FROM cc_sessions ORDER BY started_at DESC LIMIT ?",
        (limit,),
    )

    results = []
    for row in rows:
        r = dict(row)
        label = r.get("channel", "session")
        model = r.get("model", "")
        if model:
            label = f"{label} ({model})"
        results.append({
            "id": f"genesis-{r['id']}",
            "name": label,
            "status": r.get("status", "unknown"),
            "model": r.get("model"),
            "channel": r.get("channel"),
            "started_at": r.get("started_at"),
            "session_type": r.get("session_type", r.get("source_tag", "")),
        })
    return jsonify(results)


@blueprint.route("/api/genesis/ui/memory/stats")
@_async_route
async def ui_memory_stats():
    """Return counts per memory type for the Genesis memory browser."""
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.db is None:
        return jsonify({"episodic": 0, "procedural": 0, "observations": 0})

    # episodic memories live in Qdrant (not SQLite) — always 0 from DB perspective
    stats: dict = {"episodic": 0}
    for table, key in [
        ("procedural_memory", "procedural"),
        ("observations", "observations"),
    ]:
        try:
            rows = await rt.db.execute_fetchall(f"SELECT COUNT(*) FROM {table}")
            stats[key] = rows[0][0] if rows else 0
        except Exception:
            logger.exception("Failed to count %s", table)
            stats[key] = 0
    return jsonify(stats)


@blueprint.route("/api/genesis/ui/memory/search")
@_async_route
async def ui_memory_search():
    """Search Genesis memory across types."""
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.db is None:
        return jsonify({"results": [], "total": 0})

    query = request.args.get("q", "")
    mem_type = request.args.get("type", "observations")
    limit = min(request.args.get("limit", 20, type=int), 100)

    table_map = {
        "procedural": ("procedural_memory", "principle", "created_at"),
        "observations": ("observations", "content", "created_at"),
    }

    if mem_type not in table_map:
        return jsonify({"results": [], "total": 0})

    table, content_col, time_col = table_map[mem_type]

    rt.db.row_factory = aiosqlite.Row

    try:
        if query:
            rows = await rt.db.execute_fetchall(
                f"SELECT * FROM {table} WHERE {content_col} LIKE ? ORDER BY {time_col} DESC LIMIT ?",
                (f"%{query}%", limit),
            )
        else:
            rows = await rt.db.execute_fetchall(
                f"SELECT * FROM {table} ORDER BY {time_col} DESC LIMIT ?",
                (limit,),
            )
    except Exception:
        logger.exception("Failed to search %s", table)
        return jsonify({"results": [], "total": 0})

    results = [dict(r) for r in rows]
    return jsonify({"results": results, "total": len(results)})


@blueprint.route("/api/genesis/ui/inbox")
@_async_route
async def ui_inbox():
    """Return Genesis inbox items."""
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.db is None:
        return jsonify([])

    limit = min(request.args.get("limit", 30, type=int), 100)
    status = request.args.get("status")

    rt.db.row_factory = aiosqlite.Row
    if status:
        rows = await rt.db.execute_fetchall(
            "SELECT * FROM inbox_items WHERE status = ? ORDER BY created_at DESC LIMIT ?",
            (status, limit),
        )
    else:
        rows = await rt.db.execute_fetchall(
            "SELECT * FROM inbox_items ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )

    return jsonify([dict(r) for r in rows])


@blueprint.route("/api/genesis/ui/tasks")
@_async_route
async def ui_tasks():
    """Return Genesis scheduled jobs and subsystem health for the tasks modal."""
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped:
        return jsonify({"jobs": []})

    jobs = []
    if rt.db is not None:
        rt.db.row_factory = aiosqlite.Row
        try:
            rows = await rt.db.execute_fetchall(
                """SELECT subsystem, MAX(timestamp) as last_heartbeat, COUNT(*) as total_beats
                   FROM events
                   WHERE event_type = 'heartbeat'
                   GROUP BY subsystem
                   ORDER BY last_heartbeat DESC"""
            )
            for row in rows:
                r = dict(row)
                jobs.append({
                    "name": r["subsystem"],
                    "last_run": r["last_heartbeat"],
                    "total_runs": r["total_beats"],
                })
        except Exception:
            logger.exception("Failed to fetch task heartbeats")

    return jsonify({"jobs": jobs})
