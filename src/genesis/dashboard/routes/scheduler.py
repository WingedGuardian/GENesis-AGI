"""Scheduler state routes — system + user job visibility."""

from __future__ import annotations

import logging

from flask import jsonify, request

from genesis.dashboard._blueprint import _async_route, blueprint

logger = logging.getLogger(__name__)

# Maps scheduler attribute names to subsystem display names
_SCHEDULER_SUBSYSTEMS = {
    "_surplus_scheduler": "Surplus",
    "_outreach_scheduler": "Outreach",
    "_reflection_scheduler": "Reflection",
    "_awareness_loop": "Awareness",
    "_ego_cadence_manager": "Ego (User)",
    "_genesis_ego_cadence_manager": "Ego (Genesis)",
}


def _extract_apscheduler_jobs(component, subsystem: str) -> list[dict]:
    """Extract job info from an APScheduler-backed component."""
    scheduler = getattr(component, "_scheduler", None)
    if scheduler is None or not getattr(scheduler, "running", False):
        return []

    jobs = []
    try:
        for job in scheduler.get_jobs():
            trigger_str = str(job.trigger) if job.trigger else "unknown"
            # Clean up trigger string for display
            if "cron" in trigger_str.lower():
                trigger_type = "cron"
            elif "interval" in trigger_str.lower():
                trigger_type = "interval"
            else:
                trigger_type = "other"

            next_run = None
            if job.next_run_time:
                next_run = job.next_run_time.isoformat()

            jobs.append({
                "id": job.id,
                "subsystem": subsystem,
                "trigger_type": trigger_type,
                "trigger_str": trigger_str,
                "next_run_time": next_run,
            })
    except Exception:
        logger.debug("Failed to extract jobs from %s", subsystem, exc_info=True)

    return jobs


@blueprint.route("/api/genesis/scheduler/system")
@_async_route
async def system_scheduler_jobs():
    """Return all APScheduler jobs across all subsystem schedulers."""
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped:
        return jsonify({"subsystems": []})

    subsystems: dict[str, list[dict]] = {}

    for attr_name, display_name in _SCHEDULER_SUBSYSTEMS.items():
        component = getattr(rt, attr_name, None)
        if component is None:
            continue
        jobs = _extract_apscheduler_jobs(component, display_name)
        if jobs:
            subsystems[display_name] = jobs

    # Also include user job scheduler
    user_job_sched = getattr(rt, "_user_job_scheduler", None)
    if user_job_sched is not None:
        user_jobs = _extract_apscheduler_jobs(user_job_sched, "User Jobs")
        if user_jobs:
            subsystems["User Jobs"] = user_jobs

    # Merge with job_health DB data for last_run status
    job_health: dict[str, dict] = {}
    if rt.db:
        try:
            cursor = await rt.db.execute("SELECT * FROM job_health")
            for row in await cursor.fetchall():
                row_dict = dict(row)
                job_health[row_dict["job_name"]] = row_dict
        except Exception:
            pass

    # Build grouped response
    result = []
    for subsystem_name, jobs in sorted(subsystems.items()):
        result.append({
            "name": subsystem_name,
            "count": len(jobs),
            "jobs": [
                {
                    **job,
                    "last_run": job_health.get(job["id"], {}).get("last_run"),
                    "total_runs": job_health.get(job["id"], {}).get("total_runs", 0),
                    "consecutive_failures": job_health.get(job["id"], {}).get("consecutive_failures", 0),
                }
                for job in jobs
            ],
        })

    return jsonify({"subsystems": result, "total_jobs": sum(s["count"] for s in result)})


@blueprint.route("/api/genesis/scheduler/user")
@_async_route
async def user_scheduler_jobs():
    """Return user jobs from DB with status and history."""
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.db is None:
        return jsonify({"jobs": []})

    from genesis.db.crud import user_jobs as crud

    jobs = await crud.list_jobs(rt.db)
    return jsonify({
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
    })


@blueprint.route("/api/genesis/scheduler/user/<job_id>/control", methods=["POST"])
@_async_route
async def user_job_control_endpoint(job_id: str):
    """Control a user job: pause, resume, run_now, delete."""
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    scheduler = getattr(rt, "_user_job_scheduler", None)
    if scheduler is None:
        return jsonify({"error": "User job scheduler not available"}), 503

    data = request.get_json(silent=True) or {}
    action = data.get("action", "")

    valid_actions = ("pause", "resume", "run_now", "delete")
    if action not in valid_actions:
        return jsonify({"error": f"Invalid action. Must be one of: {', '.join(valid_actions)}"}), 400

    try:
        if action == "pause":
            ok = await scheduler.pause_job(job_id)
            return jsonify({"success": ok, "action": "paused"})
        elif action == "resume":
            ok = await scheduler.resume_job(job_id)
            return jsonify({"success": ok, "action": "resumed"})
        elif action == "run_now":
            session_id = await scheduler.run_now(job_id)
            return jsonify({"success": session_id is not None, "session_id": session_id})
        elif action == "delete":
            ok = await scheduler.remove_job(job_id)
            return jsonify({"success": ok, "action": "deleted"})
    except Exception as exc:
        logger.error("User job control failed: %s", exc, exc_info=True)
        return jsonify({"error": str(exc)}), 500

    return jsonify({"error": "Unknown action"}), 400
