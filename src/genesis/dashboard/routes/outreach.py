"""Outreach messages and surplus detail routes."""

from __future__ import annotations

from datetime import UTC, datetime

import aiosqlite
from flask import jsonify, request

from genesis.dashboard._blueprint import _async_route, blueprint


@blueprint.route("/api/genesis/outreach/messages")
@_async_route
async def outreach_messages():
    """Return recent outreach messages with full content."""
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.db is None:
        return jsonify([])

    rt.db.row_factory = aiosqlite.Row
    limit = request.args.get("limit", 20, type=int)
    cursor = await rt.db.execute(
        """SELECT id, category, signal_type, topic, channel, message_content,
                  delivered_at, engagement_outcome, created_at
           FROM outreach_history
           ORDER BY created_at DESC LIMIT ?""",
        (limit,),
    )
    rows = await cursor.fetchall()
    return jsonify([dict(r) for r in rows])


@blueprint.route("/api/genesis/outreach/<msg_id>/engage", methods=["POST"])
@_async_route
async def outreach_engage(msg_id: str):
    """Record user engagement with an outreach message."""
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.db is None:
        return jsonify({"error": "not bootstrapped"}), 503

    data = request.get_json(silent=True) or {}
    outcome = data.get("outcome", "engaged")
    response = data.get("response", "")

    now = datetime.now(UTC).isoformat()
    await rt.db.execute(
        """UPDATE outreach_history
           SET engagement_outcome = ?, user_response = ?, action_taken = ?
           WHERE id = ?""",
        (outcome, response, now, msg_id),
    )
    await rt.db.commit()
    return jsonify({"ok": True})


@blueprint.route("/api/genesis/surplus/detail")
@_async_route
async def surplus_detail():
    """Return detailed surplus task history and queue contents."""
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.db is None:
        return jsonify({"error": "not bootstrapped"}), 503

    rt.db.row_factory = aiosqlite.Row

    cursor = await rt.db.execute(
        """SELECT id, task_type, status, compute_tier, priority, drive_alignment,
                  created_at, started_at, completed_at, failure_reason, attempt_count
           FROM surplus_tasks
           ORDER BY created_at DESC LIMIT 100"""
    )
    all_tasks = []
    stats = {"completed": 0, "failed": 0, "pending": 0, "stub_completions": 0}
    failure_reasons: dict[str, int] = {}
    for r in await cursor.fetchall():
        task = dict(r)
        stats[task["status"]] = stats.get(task["status"], 0) + 1
        if task.get("started_at") and task.get("completed_at"):
            try:
                start = datetime.fromisoformat(task["started_at"])
                end = datetime.fromisoformat(task["completed_at"])
                dur_s = (end - start).total_seconds()
                task["duration_s"] = round(dur_s, 2)
                if dur_s < 1.0 and task["status"] == "completed":
                    task["stub"] = True
                    stats["stub_completions"] += 1
            except (ValueError, TypeError):
                task["duration_s"] = None
        if task.get("failure_reason"):
            fr = task["failure_reason"]
            failure_reasons[fr] = failure_reasons.get(fr, 0) + 1
        all_tasks.append(task)

    try:
        from genesis.surplus.types import ComputeTier, TaskType
        catalog = [{"type": t.value, "tier": ComputeTier.FREE_API.value} for t in TaskType]
    except Exception:
        catalog = []

    return jsonify({
        "tasks": all_tasks,
        "stats": stats,
        "failure_reasons": failure_reasons,
        "catalog": catalog,
    })
