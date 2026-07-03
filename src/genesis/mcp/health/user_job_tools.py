"""MCP tools for user job management."""

from __future__ import annotations

import logging

from genesis.cc.types import VALID_EFFORT_NAMES, VALID_MODEL_NAMES
from genesis.mcp.health import mcp

logger = logging.getLogger(__name__)

# Module-level state — wired at runtime via init_user_job_tools()
_db = None
_scheduler = None


def init_user_job_tools(*, db=None, scheduler=None) -> None:
    """Wire user job MCP tools at runtime."""
    global _db, _scheduler
    _db = db
    _scheduler = scheduler
    logger.info("User job MCP tools wired")


@mcp.tool()
async def user_job_create(
    title: str,
    cron_expression: str,
    dispatch_prompt: str,
    description: str | None = None,
    job_type: str = "generic",
    profile: str = "observe",
    model: str = "sonnet",
    effort: str = "medium",
) -> dict:
    """Create a new scheduled user job.

    Args:
        title: Human-readable job name
        cron_expression: 5-field cron expression (min hour dom month dow).
            Examples: "0 2 * * 0" (Sunday 2am), "0 9 * * *" (daily 9am)
        dispatch_prompt: The prompt to run in the background CC session
        description: Optional description of what the job does
        job_type: Job category (e.g., "generic", "maintenance")
        profile: CC session profile — any registered DirectSession profile
            (e.g. observe, research, interact, campaign, steward)
        model: CC model — sonnet, opus, or haiku
        effort: CC effort level — low, medium, high
    """
    if _db is None:
        return {"error": "Database not initialized"}
    if _scheduler is None:
        return {"error": "User job scheduler not initialized"}

    # Validate inputs before persisting. Use the live profile registry
    # (VALID_PROFILES, incl. any install-local overlay profiles) rather than a
    # stale hardcoded subset — matches direct_session_tools.py.
    from genesis.cc.direct_session import VALID_PROFILES

    _VALID_MODELS = VALID_MODEL_NAMES
    _VALID_EFFORTS = VALID_EFFORT_NAMES

    if profile not in VALID_PROFILES:
        return {"error": f"Invalid profile '{profile}'. Must be one of: {', '.join(sorted(VALID_PROFILES))}"}
    if model not in _VALID_MODELS:
        return {"error": f"Invalid model '{model}'. Must be one of: {', '.join(sorted(_VALID_MODELS))}"}
    if effort not in _VALID_EFFORTS:
        return {"error": f"Invalid effort '{effort}'. Must be one of: {', '.join(sorted(_VALID_EFFORTS))}"}

    # Validate cron expression
    try:
        from apscheduler.triggers.cron import CronTrigger
        CronTrigger.from_crontab(cron_expression)
    except (ValueError, KeyError) as e:
        return {"error": f"Invalid cron expression '{cron_expression}': {e}"}

    from genesis.db.crud import user_jobs as crud

    try:
        job_id = await crud.create_job(
            _db,
            title=title,
            cron_expression=cron_expression,
            dispatch_prompt=dispatch_prompt,
            description=description,
            job_type=job_type,
            profile=profile,
            model=model,
            effort=effort,
        )

        # Register with APScheduler
        await _scheduler.add_job(job_id)

        return {
            "success": True,
            "job_id": job_id,
            "title": title,
            "cron_expression": cron_expression,
            "profile": profile,
            "model": model,
        }
    except Exception as exc:
        logger.error("Failed to create user job: %s", exc, exc_info=True)
        return {"error": f"Failed to create job: {exc}"}


@mcp.tool()
async def user_job_list() -> dict:
    """List all user jobs with their status and last run info."""
    if _db is None:
        return {"error": "Database not initialized"}

    from genesis.db.crud import user_jobs as crud

    try:
        jobs = await crud.list_jobs(_db)
        return {
            "success": True,
            "count": len(jobs),
            "jobs": [
                {
                    "id": j["id"],
                    "title": j["title"],
                    "description": j.get("description"),
                    "cron_expression": j["cron_expression"],
                    "job_type": j.get("job_type", "generic"),
                    "status": j["status"],
                    "profile": j.get("profile", "observe"),
                    "model": j.get("model", "sonnet"),
                    "last_run_at": j.get("last_run_at"),
                    "last_status": j.get("last_status"),
                    "failure_count": j.get("failure_count", 0),
                    "created_at": j.get("created_at"),
                }
                for j in jobs
            ],
        }
    except Exception as exc:
        logger.error("Failed to list user jobs: %s", exc, exc_info=True)
        return {"error": f"Failed to list jobs: {exc}"}


@mcp.tool()
async def user_job_control(
    job_id: str,
    action: str,
) -> dict:
    """Control a user job: pause, resume, run_now, or delete.

    Args:
        job_id: The job ID to control
        action: One of: pause, resume, run_now, delete
    """
    if _scheduler is None:
        return {"error": "User job scheduler not initialized"}

    valid_actions = ("pause", "resume", "run_now", "delete")
    if action not in valid_actions:
        return {"error": f"Invalid action '{action}'. Must be one of: {', '.join(valid_actions)}"}

    try:
        if action == "pause":
            ok = await _scheduler.pause_job(job_id)
            return {"success": ok, "action": "paused", "job_id": job_id}
        elif action == "resume":
            ok = await _scheduler.resume_job(job_id)
            return {"success": ok, "action": "resumed", "job_id": job_id}
        elif action == "run_now":
            session_id = await _scheduler.run_now(job_id)
            return {
                "success": session_id is not None,
                "action": "dispatched",
                "job_id": job_id,
                "session_id": session_id,
            }
        elif action == "delete":
            ok = await _scheduler.remove_job(job_id)
            return {"success": ok, "action": "deleted", "job_id": job_id}
    except Exception as exc:
        logger.error("User job control failed: %s", exc, exc_info=True)
        return {"error": f"Failed to {action} job: {exc}"}

    return {"error": "Unknown action"}  # pragma: no cover — guarded by valid_actions check


@mcp.tool()
async def user_job_history(
    job_id: str,
    limit: int = 10,
) -> dict:
    """Get recent run history for a user job.

    Args:
        job_id: The job ID to get history for
        limit: Maximum number of runs to return (default 10)
    """
    if _db is None:
        return {"error": "Database not initialized"}

    from genesis.db.crud import user_jobs as crud

    try:
        job = await crud.get_job(_db, job_id)
        if not job:
            return {"error": f"Job {job_id} not found"}

        runs = await crud.get_run_history(_db, job_id, limit=limit)
        return {
            "success": True,
            "job_id": job_id,
            "title": job["title"],
            "runs": [
                {
                    "id": r["id"],
                    "status": r["status"],
                    "session_id": r.get("session_id"),
                    "started_at": r.get("started_at"),
                    "completed_at": r.get("completed_at"),
                    "error_message": r.get("error_message"),
                }
                for r in runs
            ],
        }
    except Exception as exc:
        logger.error("Failed to get job history: %s", exc, exc_info=True)
        return {"error": f"Failed to get history: {exc}"}
