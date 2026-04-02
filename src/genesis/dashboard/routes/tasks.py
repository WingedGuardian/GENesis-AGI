"""Task execution viewer routes.

Exposes task state, step details, and control actions (pause/resume/cancel)
for the dashboard.  Backed by the task executor and task_states/task_steps
CRUD modules.
"""

from __future__ import annotations

import contextlib
import json
import logging

from flask import jsonify, request

from genesis.dashboard._blueprint import _async_route, blueprint

logger = logging.getLogger(__name__)


@blueprint.route("/api/genesis/tasks")
@_async_route
async def task_list():
    """Return active and recent tasks.

    Query params:
        include_completed – "true" to include completed/failed (default false)
        limit – max results (default 50)
    """
    from genesis.db.crud import task_states
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.db is None:
        return jsonify({"tasks": [], "active": {}})

    include_completed = request.args.get("include_completed", "false").lower() == "true"
    limit = min(request.args.get("limit", 50, type=int), 200)

    try:
        tasks = await task_states.list_all_recent(rt.db, limit=limit)
    except Exception:
        logger.error("Failed to list tasks", exc_info=True)
        return jsonify({"tasks": [], "active": {}})

    # Filter if not including completed
    if not include_completed:
        terminal = {"completed", "failed", "cancelled"}
        tasks = [t for t in tasks if t.get("current_phase", "").lower() not in terminal]

    # Get live active tasks from executor (if available)
    active = {}
    if rt.task_executor is not None:
        active = rt.task_executor.get_active_tasks()

    return jsonify({"tasks": tasks, "active": active})


@blueprint.route("/api/genesis/tasks/<task_id>")
@_async_route
async def task_detail(task_id: str):
    """Return full task state with steps."""
    from genesis.db.crud import task_states, task_steps
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.db is None:
        return jsonify({"error": "Not bootstrapped"}), 503

    task = await task_states.get_by_id(rt.db, task_id)
    if task is None:
        return jsonify({"error": f"Task {task_id} not found"}), 404

    steps = await task_steps.get_steps_for_task(rt.db, task_id)

    # Parse JSON fields safely
    for field in ("blockers", "outputs", "decisions"):
        if task.get(field):
            with contextlib.suppress(json.JSONDecodeError, ValueError, TypeError):
                task[field] = json.loads(task[field])

    # Enrich steps with result preview
    enriched_steps = []
    for s in steps:
        step = dict(s)
        # Parse result_json
        if step.get("result_json"):
            with contextlib.suppress(json.JSONDecodeError, ValueError, TypeError):
                step["result_json"] = json.loads(step["result_json"])
        enriched_steps.append(step)

    # Check if paused (per-task)
    is_paused = False
    if rt.task_executor is not None:
        is_paused = rt.task_executor.is_task_paused(task_id)

    return jsonify({
        "task": task,
        "steps": enriched_steps,
        "is_paused": is_paused,
    })


@blueprint.route("/api/genesis/tasks/<task_id>/pause", methods=["POST"])
def task_pause(task_id: str):
    """Pause a running task at its next checkpoint."""
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if rt.task_executor is None:
        return jsonify({"error": "Task executor not available"}), 503

    success = rt.task_executor.pause_task(task_id)
    if success:
        return jsonify({"status": "pause_requested", "task_id": task_id})
    return jsonify({"error": "Task not found or not active"}), 404


@blueprint.route("/api/genesis/tasks/<task_id>/resume", methods=["POST"])
def task_resume(task_id: str):
    """Resume a paused task."""
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if rt.task_executor is None:
        return jsonify({"error": "Task executor not available"}), 503

    success = rt.task_executor.resume_task(task_id)
    if success:
        return jsonify({"status": "resumed", "task_id": task_id})
    return jsonify({"error": "Task not found or not paused"}), 404


@blueprint.route("/api/genesis/tasks/<task_id>/cancel", methods=["POST"])
def task_cancel(task_id: str):
    """Cancel a running task."""
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if rt.task_executor is None:
        return jsonify({"error": "Task executor not available"}), 503

    success = rt.task_executor.cancel_task(task_id)
    if success:
        return jsonify({"status": "cancel_requested", "task_id": task_id})
    return jsonify({"error": "Task not found or not active"}), 404
