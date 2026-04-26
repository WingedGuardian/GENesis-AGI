"""Unified Work tab route — aggregates tasks, follow-ups, and background sessions."""

from __future__ import annotations

import logging

from flask import jsonify, request

from genesis.dashboard._blueprint import _async_route, blueprint

logger = logging.getLogger(__name__)


@blueprint.route("/api/genesis/work")
@_async_route
async def unified_work():
    """Return a unified work view aggregating tasks, follow-ups, and bg sessions.

    Query params:
        view  – 'active' (default) or 'history'
        type  – optional filter: 'task', 'follow_up', 'session'
        limit – max items per source (default 50, capped at 200)
    """
    from genesis.db.crud import follow_ups, task_states
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.db is None:
        return jsonify({"items": [], "counts": {}})

    view = request.args.get("view", "active")
    type_filter = request.args.get("type") or None
    limit = max(1, min(request.args.get("limit", 50, type=int), 200))

    items: list[dict] = []
    counts: dict = {}

    # --- Tasks ---
    if type_filter in (None, "task"):
        try:
            if view == "active":
                tasks = await task_states.list_active(rt.db)
            else:
                tasks = await task_states.list_all_recent(rt.db, limit=limit)
            for t in tasks:
                items.append({
                    "id": t["task_id"],
                    "type": "task",
                    "status": t.get("current_phase", "unknown"),
                    "description": t.get("description", ""),
                    "created_at": t.get("created_at", ""),
                    "updated_at": t.get("updated_at", ""),
                    "source": t.get("source_session_id", ""),
                    "raw": t,
                })
            active_tasks = await task_states.list_active(rt.db)
            completed = await _count_tasks_by_phase(rt.db, "completed")
            failed = await _count_tasks_by_phase(rt.db, "failed")
            counts["tasks"] = {
                "active": len(active_tasks),
                "completed": completed,
                "failed": failed,
            }
        except Exception:
            logger.error("Failed to fetch tasks for work view", exc_info=True)
            counts["tasks"] = {"active": 0, "completed": 0, "failed": 0}

    # --- Follow-ups ---
    if type_filter in (None, "follow_up"):
        try:
            if view == "active":
                fups = await follow_ups.get_by_status(rt.db, "pending")
                fups += await follow_ups.get_by_status(rt.db, "in_progress")
                fups += await follow_ups.get_by_status(rt.db, "blocked")
            else:
                fups = await follow_ups.get_recent(rt.db, limit=limit)
            for f in fups:
                items.append({
                    "id": f["id"],
                    "type": "follow_up",
                    "status": f.get("status", "pending"),
                    "description": f.get("content", ""),
                    "created_at": f.get("created_at", ""),
                    "updated_at": f.get("completed_at") or f.get("created_at", ""),
                    "priority": f.get("priority", "medium"),
                    "strategy": f.get("strategy", ""),
                    "source": f.get("source", ""),
                    "reason": f.get("reason", ""),
                    "resolution_notes": f.get("resolution_notes", ""),
                    "blocked_reason": f.get("blocked_reason", ""),
                    "raw": f,
                })
            fu_counts = await follow_ups.get_summary_counts(rt.db)
            counts["follow_ups"] = fu_counts
        except Exception:
            logger.error("Failed to fetch follow-ups for work view", exc_info=True)
            counts["follow_ups"] = {}

    # --- Background sessions ---
    if type_filter in (None, "session"):
        try:
            if view == "active":
                cursor = await rt.db.execute(
                    """SELECT id, session_type, model, status, source_tag, channel,
                              cost_usd, started_at, last_activity_at
                       FROM cc_sessions
                       WHERE session_type != 'foreground' AND status = 'active'
                       ORDER BY started_at DESC LIMIT ?""",
                    (limit,),
                )
            else:
                cursor = await rt.db.execute(
                    """SELECT id, session_type, model, status, source_tag, channel,
                              cost_usd, started_at, last_activity_at
                       FROM cc_sessions
                       WHERE session_type != 'foreground'
                       ORDER BY started_at DESC LIMIT ?""",
                    (limit,),
                )
            cols = [d[0] for d in cursor.description]
            rows = await cursor.fetchall()
            for r in rows:
                sess = dict(zip(cols, r, strict=False))
                items.append({
                    "id": sess["id"],
                    "type": "session",
                    "status": sess.get("status", "unknown"),
                    "description": sess.get("source_tag") or sess.get("channel") or "background session",
                    "created_at": sess.get("started_at", ""),
                    "updated_at": sess.get("last_activity_at") or sess.get("started_at", ""),
                    "session_type": sess.get("session_type", ""),
                    "model": sess.get("model", ""),
                    "cost_usd": sess.get("cost_usd"),
                    "raw": sess,
                })
            # Count active bg sessions
            count_cursor = await rt.db.execute(
                "SELECT COUNT(*) FROM cc_sessions WHERE session_type != 'foreground' AND status = 'active'"
            )
            active_bg = (await count_cursor.fetchone())[0]
            counts["sessions"] = {"active_bg": active_bg}
        except Exception:
            logger.error("Failed to fetch sessions for work view", exc_info=True)
            counts["sessions"] = {"active_bg": 0}

    # Sort all items by most recent activity
    items.sort(key=lambda x: x.get("updated_at") or x.get("created_at") or "", reverse=True)

    return jsonify({
        "items": items[:limit],
        "counts": counts,
    })


async def _count_tasks_by_phase(db, phase: str) -> int:
    """Count tasks in a specific phase."""
    cursor = await db.execute(
        "SELECT COUNT(*) FROM task_states WHERE current_phase = ?",
        (phase,),
    )
    row = await cursor.fetchone()
    return row[0] if row else 0
