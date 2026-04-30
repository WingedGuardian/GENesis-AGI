"""MCP tools for autonomous task management.

Provides task_submit, task_list, task_detail, and task_control tools
for the health MCP server.

When running inside the main Genesis server, these tools use the wired
dispatcher/executor/db references.  When running as a CC child process
(MCP server), they fall back to direct DB connections for read operations
and DB-only task creation for submit (the main server's dispatch_cycle
picks up PENDING tasks).
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite

from genesis.db.connection import BUSY_TIMEOUT_MS
from genesis.mcp.health import mcp

logger = logging.getLogger(__name__)

# Module-level state, wired by init_task_tools() when running in-server.
# When running as MCP child process, these remain None and we use _get_db().
_dispatcher = None
_executor = None
_db = None

# DB path for fallback connections (matches manifest.py pattern)
_DB_PATH = Path.home() / "genesis" / "data" / "genesis.db"

# Allowed directories for plan files (must match dispatcher.py)
_ALLOWED_PLAN_DIRS = [
    Path.home() / ".genesis" / "plans",
    Path.home() / ".claude" / "plans",
]

# Required sections in plan files (must match dispatcher.py)
REQUIRED_PLAN_SECTIONS = [
    "## Requirements",
    "## Steps",
    "## Success Criteria",
    "## Risks and Failure Modes",
]


def _validate_plan_content(path: Path) -> list[str]:
    """Check plan file contains required TASK_INTAKE.md sections.

    Returns list of missing section headers (empty if valid).
    """
    lines = set(path.read_text().splitlines())
    return [s for s in REQUIRED_PLAN_SECTIONS if s not in lines]


def init_task_tools(dispatcher, executor, *, db=None) -> None:
    """Wire dispatcher and executor references. Called from runtime init."""
    global _dispatcher, _executor, _db
    _dispatcher = dispatcher
    _executor = executor
    _db = db
    logger.info("Task MCP tools wired to dispatcher + executor")


async def _get_db() -> aiosqlite.Connection:
    """Open a direct DB connection for MCP fallback reads/writes."""
    db = await aiosqlite.connect(str(_DB_PATH))
    db.row_factory = aiosqlite.Row
    await db.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    return db


# ---------------------------------------------------------------------------
# Implementation functions (testable without FastMCP)
# ---------------------------------------------------------------------------


async def _impl_intake_complete() -> dict:
    """Generate a one-time intake token after /task guided intake.

    Returns {"token": "..."} — pass to task_submit as intake_token.
    This is a procedural friction gate: any code with DB access can
    generate a token.  The goal is preventing accidental bypass of
    the /task intake process, not adversarial security.
    """
    token = uuid.uuid4().hex
    now = datetime.now(UTC).isoformat()
    expires = (datetime.now(UTC) + timedelta(hours=2)).isoformat()

    db = _db
    own_db = False
    if db is None:
        try:
            db = await _get_db()
            own_db = True
        except Exception as exc:
            logger.error("intake_complete DB connection failed", exc_info=True)
            return {"error": f"Database unavailable: {type(exc).__name__}: {exc}"}
    try:
        await db.execute(
            "INSERT INTO intake_tokens (token, created_at, expires_at) VALUES (?,?,?)",
            (token, now, expires),
        )
        await db.commit()
        return {"token": token}
    except Exception as exc:
        logger.error("intake_complete failed", exc_info=True)
        return {"error": f"Failed to generate intake token: {exc}"}
    finally:
        if own_db:
            await db.close()


async def _impl_task_submit(plan_path: str, description: str, intake_token: str | None = None) -> dict:
    """Submit a task for autonomous execution.

    When the dispatcher is wired (in-server), uses it directly.
    When running as MCP child process, creates the DB row directly
    and the main server's dispatch_cycle picks it up.
    """
    if not plan_path or not plan_path.strip():
        return {"error": "plan_path is required"}
    if not description or not description.strip():
        return {"error": "description is required"}

    plan_path = plan_path.strip()
    description = description.strip()

    # In-server path: use dispatcher directly
    if _dispatcher is not None:
        try:
            task_id = await _dispatcher.submit(plan_path, description, intake_token=intake_token)
            return {"task_id": task_id, "status": "dispatched"}
        except ValueError as exc:
            return {"error": str(exc)}
        except FileNotFoundError as exc:
            return {"error": f"Plan file not found: {exc}"}
        except Exception as exc:
            logger.error("task_submit failed", exc_info=True)
            return {"error": f"Failed to submit task: {type(exc).__name__}: {exc}"}

    # MCP fallback: validate + create DB row, server picks up via dispatch_cycle
    try:
        resolved = Path(plan_path).expanduser().resolve()
        if not any(resolved.is_relative_to(d) for d in _ALLOWED_PLAN_DIRS):
            return {
                "error": f"Plan path outside allowed directories: {plan_path}. "
                f"Allowed: {[str(d) for d in _ALLOWED_PLAN_DIRS]}",
            }
        if not resolved.exists():
            return {"error": f"Plan file not found: {plan_path}"}
    except Exception as exc:
        return {"error": f"Invalid plan path: {exc}"}

    # Content validation: check required sections
    missing = _validate_plan_content(resolved)
    if missing:
        return {
            "error": f"Plan file missing required sections: {', '.join(missing)}. "
            "See TASK_INTAKE.md format. Use /task for guided intake.",
        }

    task_id = f"t-{uuid.uuid4().hex[:12]}"
    now = datetime.now(UTC).isoformat()

    try:
        from genesis.db.crud import task_states

        db = await _get_db()
        try:
            # task_states.create() auto-commits
            await task_states.create(
                db,
                task_id=task_id,
                description=description,
                current_phase="pending",
                decisions=None,
                blockers=None,
                outputs=str(resolved),
                session_id=None,
                intake_token=intake_token,
                created_at=now,
            )
        finally:
            await db.close()

        return {
            "task_id": task_id,
            "status": "pending",
            "note": "Task created. The server will pick it up on next dispatch cycle (~2 min).",
        }
    except Exception as exc:
        logger.error("task_submit DB fallback failed", exc_info=True)
        return {"error": f"Failed to submit task: {type(exc).__name__}: {exc}"}


async def _impl_task_list(include_completed: bool = False) -> dict:
    """List tasks with their current status."""
    from genesis.db.crud import task_states

    # Use wired DB if available, otherwise open direct connection
    db = _db
    own_db = False
    if db is None:
        try:
            db = await _get_db()
            own_db = True
        except Exception as exc:
            logger.error("task_list DB connection failed", exc_info=True)
            return {"error": f"Database unavailable: {type(exc).__name__}: {exc}"}

    try:
        if include_completed:
            tasks = await task_states.list_all_recent(db, limit=50)
        else:
            tasks = await task_states.list_active(db)

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
    finally:
        if own_db:
            await db.close()


async def _impl_task_detail(task_id: str) -> dict:
    """Get full task state including steps and blockers."""
    from genesis.db.crud import task_states, task_steps

    # Use wired DB if available, otherwise open direct connection
    db = _db
    own_db = False
    if db is None:
        try:
            db = await _get_db()
            own_db = True
        except Exception as exc:
            logger.error("task_detail DB connection failed", exc_info=True)
            return {"error": f"Database unavailable: {type(exc).__name__}: {exc}"}

    try:
        task = await task_states.get_by_id(db, task_id)
        if task is None:
            return {"error": f"Task {task_id} not found"}

        steps = await task_steps.get_steps_for_task(db, task_id)

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
    finally:
        if own_db:
            await db.close()


_TASK_CONTROL_ACTIONS = {
    "pause": ("pause_task", "pause_requested", "not found or not active"),
    "resume": ("resume_task", "resumed", "not found or not paused"),
    "cancel": ("cancel_task", "cancel_requested", "not found or not active"),
}


async def _impl_task_control(task_id: str, action: str) -> dict:
    """Pause, resume, or cancel a task.

    Requires the in-server executor — these operate on in-memory state
    and cannot be mediated through DB alone.
    """
    if _executor is None:
        return {
            "error": "Task control requires the main Genesis server. "
            "Use task_list/task_detail for read-only access from any session.",
        }

    action = action.lower().strip()
    if action not in _TASK_CONTROL_ACTIONS:
        return {"error": f"Invalid action '{action}'. Must be one of: pause, resume, cancel"}

    method_name, success_status, fail_msg = _TASK_CONTROL_ACTIONS[action]
    method = getattr(_executor, method_name, None)
    if method is None:
        return {"error": f"Executor does not support '{action}'"}

    success = method(task_id)
    if success:
        return {"task_id": task_id, "status": success_status}
    return {"error": f"Task {task_id} {fail_msg}"}


# ---------------------------------------------------------------------------
# MCP tool decorators
# ---------------------------------------------------------------------------


@mcp.tool()
async def intake_complete() -> dict:
    """Generate a one-time intake token after completing /task guided intake.

    Call this AFTER the plan is written and approved, BEFORE task_submit.
    Returns a token that must be passed to task_submit as intake_token.
    Tokens expire after 2 hours and can only be used once.
    """
    return await _impl_intake_complete()


@mcp.tool()
async def task_submit(plan_path: str, description: str, intake_token: str = "") -> dict:
    """Submit a task for autonomous background execution.

    Provide the path to an approved plan file, a brief description, and
    the intake_token from intake_complete. Genesis will execute the plan
    autonomously in a background session, using adversarial review before
    delivering results.

    Plan files must be in ~/.genesis/plans/ or ~/.claude/plans/.
    The intake_token is required — generate it via intake_complete after
    completing the /task guided intake process.
    """
    return await _impl_task_submit(plan_path, description, intake_token=intake_token or None)


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
async def task_control(task_id: str, action: str) -> dict:
    """Control a running task: pause, resume, or cancel.

    Actions:
    - **pause**: Pause at next checkpoint. Use action='resume' to continue.
    - **resume**: Resume a paused task from where it left off.
    - **cancel**: Cancel the task. It will be marked cancelled at next checkpoint
      and any worktree will be cleaned up.

    Note: requires the main Genesis server process. Read-only tools
    (task_list, task_detail) work from any session.
    """
    return await _impl_task_control(task_id, action)
