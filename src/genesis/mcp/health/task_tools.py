"""MCP tools for autonomous task management.

Provides task_submit, task_list, task_detail, task_pause, task_resume,
and task_cancel tools for the health MCP server.
"""

from __future__ import annotations

import json
import logging

from genesis.mcp.health import mcp

logger = logging.getLogger(__name__)

# Module-level state, wired by init_task_tools()
_dispatcher = None
_executor = None
_db = None


def init_task_tools(dispatcher, executor, *, db=None) -> None:
    """Wire dispatcher and executor references. Called from runtime init."""
    global _dispatcher, _executor, _db
    _dispatcher = dispatcher
    _executor = executor
    _db = db
    logger.info("Task MCP tools wired to dispatcher + executor")


# ---------------------------------------------------------------------------
# Implementation functions (testable without FastMCP)
# ---------------------------------------------------------------------------


async def _impl_task_submit(plan_path: str, description: str) -> dict:
    """Submit a task for autonomous execution."""
    if _dispatcher is None:
        return {"error": "Task executor not initialized"}

    if not plan_path or not plan_path.strip():
        return {"error": "plan_path is required"}
    if not description or not description.strip():
        return {"error": "description is required"}

    try:
        task_id = await _dispatcher.submit(plan_path.strip(), description.strip())
        return {"task_id": task_id, "status": "dispatched"}
    except ValueError as exc:
        return {"error": f"Invalid plan path: {exc}"}
    except FileNotFoundError as exc:
        return {"error": f"Plan file not found: {exc}"}
    except Exception as exc:
        logger.error("task_submit failed", exc_info=True)
        return {"error": f"Failed to submit task: {type(exc).__name__}: {exc}"}


async def _impl_task_list(include_completed: bool = False) -> dict:
    """List tasks with their current status."""
    if _dispatcher is None:
        return {"error": "Task executor not initialized"}

    from genesis.db.crud import task_states

    try:
        if include_completed:
            tasks = await task_states.list_all_recent(_db, limit=50)
        else:
            tasks = await task_states.list_active(_db)

        return {
            "tasks": [
                {
                    "task_id": t["task_id"],
                    "description": t.get("description", ""),
                    "phase": t.get("current_phase", "unknown"),
                    "created_at": t.get("created_at", ""),
                }
                for t in tasks
            ],
            "count": len(tasks),
        }
    except Exception as exc:
        logger.error("task_list failed", exc_info=True)
        return {"error": f"Failed to list tasks: {type(exc).__name__}: {exc}"}


async def _impl_task_detail(task_id: str) -> dict:
    """Get full task state including steps and blockers."""
    if _dispatcher is None:
        return {"error": "Task executor not initialized"}

    from genesis.db.crud import task_states, task_steps

    try:
        task = await task_states.get_by_id(_db, task_id)
        if task is None:
            return {"error": f"Task {task_id} not found"}

        steps = await task_steps.get_steps_for_task(_db, task_id)

        # Parse JSON fields safely
        blockers = None
        if task.get("blockers"):
            try:
                blockers = json.loads(task["blockers"])
            except (json.JSONDecodeError, ValueError):
                blockers = task["blockers"]

        outputs = None
        if task.get("outputs"):
            try:
                outputs = json.loads(task["outputs"])
            except (json.JSONDecodeError, ValueError):
                outputs = task["outputs"]

        return {
            "task_id": task["task_id"],
            "description": task.get("description", ""),
            "phase": task.get("current_phase", "unknown"),
            "created_at": task.get("created_at", ""),
            "updated_at": task.get("updated_at", ""),
            "blockers": blockers,
            "outputs": outputs,
            "steps": [
                {
                    "step_idx": s.get("step_idx"),
                    "step_type": s.get("step_type", ""),
                    "description": s.get("description", ""),
                    "status": s.get("status", ""),
                    "cost_usd": s.get("cost_usd", 0.0),
                    "result_json": s.get("result_json"),
                    "started_at": s.get("started_at"),
                    "completed_at": s.get("completed_at"),
                    "model_used": s.get("model_used", ""),
                }
                for s in steps
            ],
        }
    except Exception as exc:
        logger.error("task_detail failed for %s", task_id, exc_info=True)
        return {"error": f"Failed to get task detail: {type(exc).__name__}: {exc}"}


async def _impl_task_pause(task_id: str) -> dict:
    """Pause a running task at its next checkpoint."""
    if _executor is None:
        return {"error": "Task executor not initialized"}

    success = _executor.pause_task(task_id)
    if success:
        return {"task_id": task_id, "status": "pause_requested"}
    return {"error": f"Task {task_id} not found or not active"}


async def _impl_task_resume(task_id: str) -> dict:
    """Resume a paused task."""
    if _executor is None:
        return {"error": "Task executor not initialized"}

    success = _executor.resume_task(task_id)
    if success:
        return {"task_id": task_id, "status": "resumed"}
    return {"error": f"Task {task_id} not found or not paused"}


async def _impl_task_cancel(task_id: str) -> dict:
    """Cancel a running or paused task."""
    if _executor is None:
        return {"error": "Task executor not initialized"}

    success = _executor.cancel_task(task_id)
    if success:
        return {"task_id": task_id, "status": "cancel_requested"}
    return {"error": f"Task {task_id} not found or not active"}


# ---------------------------------------------------------------------------
# MCP tool decorators
# ---------------------------------------------------------------------------


@mcp.tool()
async def task_submit(plan_path: str, description: str) -> dict:
    """Submit a task for autonomous background execution.

    Provide the path to an approved plan file and a brief description.
    Genesis will execute the plan autonomously in a background session,
    using adversarial review before delivering results. You'll be
    notified via Telegram at key milestones and if Genesis gets stuck.

    Plan files must be in ~/.genesis/plans/ or ~/.claude/plans/.
    """
    return await _impl_task_submit(plan_path, description)


@mcp.tool()
async def task_list(include_completed: bool = False) -> dict:
    """List autonomous tasks with their current status.

    By default shows only active (non-completed) tasks.
    Set include_completed=True to see recent completed tasks too.
    """
    return await _impl_task_list(include_completed)


@mcp.tool()
async def task_detail(task_id: str) -> dict:
    """Get full details for a specific task.

    Returns the task's current phase, all step results, any blockers,
    and output artifacts. Use this to inspect task progress or debug issues.
    """
    return await _impl_task_detail(task_id)


@mcp.tool()
async def task_pause(task_id: str) -> dict:
    """Pause a running task at its next checkpoint.

    Uses the global pause mechanism (runtime.paused). Per-task pause
    is not yet implemented — this pauses all background execution.
    Use task_resume to continue.
    """
    return await _impl_task_pause(task_id)


@mcp.tool()
async def task_resume(task_id: str) -> dict:
    """Resume a paused task from where it left off."""
    return await _impl_task_resume(task_id)


@mcp.tool()
async def task_cancel(task_id: str) -> dict:
    """Cancel a running or paused task.

    The task will be marked as cancelled at its next checkpoint.
    Any worktree created for the task will be cleaned up.
    """
    return await _impl_task_cancel(task_id)
