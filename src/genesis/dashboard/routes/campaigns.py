"""Campaign management routes — list, detail, and controls.

Live, access-controlled view of the campaign subsystem (the ``campaigns`` /
``campaign_runs`` tables) plus basic controls: pause / resume / trigger / edit
(cadence, model, effort, daily cap, jitter).

Mutating controls drive the *live* ``CampaignRunner`` directly. That is safe
from a Flask worker thread because ``_async_route`` dispatches the coroutine
onto the runtime event loop (where the scheduler and the aiosqlite connection
live) via ``run_coroutine_threadsafe`` — so this is actually the right home for
schedule control, unlike the standalone MCP process where no runner is wired.

Every route is gated with ``is_authenticated()`` (a no-op when
``DASHBOARD_PASSWORD`` is unset, an enforced 403 when set) — these serve the
human UI and can change system state, so they get the same protection as the
login-gated pages, a deliberate exception to the "API routes bypass auth"
default.
"""

from __future__ import annotations

import json
import logging

from flask import jsonify, request

from genesis.dashboard._blueprint import _async_route, blueprint
from genesis.dashboard.auth import is_authenticated

logger = logging.getLogger(__name__)


def _auth_or_403():
    """Return a 403 response tuple if not authenticated, else None."""
    if not is_authenticated():
        return jsonify({"error": "authentication required"}), 403
    return None


def _runtime_or_503():
    """Return (rt, None) when ready, else (None, 503-response)."""
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.db is None:
        return None, (jsonify({"error": "Not bootstrapped"}), 503)
    return rt, None


def _parse_state(state_json: str | None) -> dict:
    if not state_json:
        return {}
    try:
        return json.loads(state_json)
    except (json.JSONDecodeError, TypeError):
        return {}


def _next_fire(runner, name: str) -> str | None:
    """Live next-fire time for a campaign's scheduler job, or None.

    Returns None when paused (no job registered) or the scheduler is down.
    """
    scheduler = getattr(runner, "_scheduler", None) if runner else None
    if scheduler is None:
        return None
    job = scheduler.get_job(f"campaign_{name}")
    if job is None or job.next_run_time is None:
        return None
    return job.next_run_time.isoformat()


@blueprint.route("/api/genesis/campaigns/list")
@_async_route
async def campaigns_list():
    """All campaigns with status, schedule, cost, and pending indicators."""
    if (resp := _auth_or_403()) is not None:
        return resp
    rt, err = _runtime_or_503()
    if err is not None:
        return err

    from datetime import UTC, datetime

    from genesis.db.crud import campaigns as crud

    runner = getattr(rt, "_campaign_runner", None)
    today = datetime.now(UTC).isoformat()[:10]

    try:
        rows = await crud.list_campaigns(rt.db)
        items = []
        for c in rows:
            state = _parse_state(c.get("state_json"))
            counts = await crud.count_runs_by_outcome(rt.db, c["id"])
            today_cost = await crud.get_daily_cost(rt.db, c["id"], today)
            items.append({
                "id": c["id"],
                "name": c["name"],
                "status": c["status"],
                "cadence": c["cron_cadence"],
                "jitter_seconds": c.get("jitter_seconds"),
                "model": c["model"],
                "effort": c["effort"],
                "profile": c["session_profile"],
                "max_daily_cost_usd": c["max_daily_cost_usd"],
                "today_cost_usd": round(today_cost, 4),
                "total_runs": c["total_runs"],
                "total_cost_usd": round(c["total_cost_usd"], 4),
                "attempts": sum(counts.values()),
                "run_counts": counts,
                "last_run_at": c.get("last_run_at"),
                "next_fire": _next_fire(runner, c["name"]),
                "pending_session": bool(state.get("_pending_session_id")),
            })
        return jsonify({"campaigns": items, "count": len(items)})
    except Exception:
        logger.exception("Campaigns list failed")
        return jsonify({"error": "Failed to list campaigns"}), 500


@blueprint.route("/api/genesis/campaigns/<name>/detail")
@_async_route
async def campaign_detail(name: str):
    """Full status for one campaign: config, visible state, recent runs."""
    if (resp := _auth_or_403()) is not None:
        return resp
    rt, err = _runtime_or_503()
    if err is not None:
        return err

    from genesis.db.crud import campaigns as crud

    runner = getattr(rt, "_campaign_runner", None)

    try:
        c = await crud.get_campaign_by_name(rt.db, name)
        if not c:
            return jsonify({"error": "Campaign not found"}), 404

        state = _parse_state(c.get("state_json"))
        visible_state = {k: v for k, v in state.items() if not k.startswith("_")}
        runs = await crud.list_runs(rt.db, c["id"], limit=15)
        counts = await crud.count_runs_by_outcome(rt.db, c["id"])

        return jsonify({
            "id": c["id"],
            "name": c["name"],
            "status": c["status"],
            "cadence": c["cron_cadence"],
            "jitter_seconds": c.get("jitter_seconds"),
            "model": c["model"],
            "effort": c["effort"],
            "profile": c["session_profile"],
            "max_daily_cost_usd": c["max_daily_cost_usd"],
            "strategy_doc_path": c["strategy_doc_path"],
            "total_runs": c["total_runs"],
            "total_cost_usd": round(c["total_cost_usd"], 4),
            "run_counts": counts,
            "last_run_at": c.get("last_run_at"),
            "next_fire": _next_fire(runner, c["name"]),
            "pending_session": bool(state.get("_pending_session_id")),
            "state": visible_state,
            "recent_runs": [
                {
                    "started_at": r["started_at"],
                    "finished_at": r.get("finished_at"),
                    "trigger_type": r["trigger_type"],
                    "outcome": r["outcome"],
                    "skip_reason": r.get("skip_reason"),
                    "summary": r.get("summary"),
                    "cost_usd": round(r["cost_usd"], 4),
                    "session_id": r.get("session_id"),
                }
                for r in runs
            ],
        })
    except Exception:
        logger.exception("Campaign detail failed for %s", name)
        return jsonify({"error": "Failed to load campaign"}), 500


async def _control(name: str, action):
    """Shared wrapper: auth + runtime guard + run a control coroutine."""
    if (resp := _auth_or_403()) is not None:
        return resp
    rt, err = _runtime_or_503()
    if err is not None:
        return err
    runner = getattr(rt, "_campaign_runner", None)
    try:
        result = await action(rt.db, runner)
        if "error" in result:
            return jsonify(result), 400
        return jsonify(result)
    except Exception:
        logger.exception("Campaign control failed for %s", name)
        return jsonify({"error": "Control action failed"}), 500


@blueprint.route("/api/genesis/campaigns/<name>/pause", methods=["POST"])
@_async_route
async def campaign_pause(name: str):
    from genesis.campaigns import control
    return await _control(name, lambda db, r: control.pause_campaign(db, r, name))


@blueprint.route("/api/genesis/campaigns/<name>/resume", methods=["POST"])
@_async_route
async def campaign_resume(name: str):
    from genesis.campaigns import control
    return await _control(name, lambda db, r: control.resume_campaign(db, r, name))


@blueprint.route("/api/genesis/campaigns/<name>/trigger", methods=["POST"])
@_async_route
async def campaign_trigger(name: str):
    from genesis.campaigns import control
    return await _control(name, lambda db, r: control.trigger_campaign(db, r, name))


@blueprint.route("/api/genesis/campaigns/<name>/update", methods=["POST"])
@_async_route
async def campaign_update(name: str):
    from genesis.campaigns import control

    body = request.get_json(silent=True) or {}

    def _opt_int(key):
        v = body.get(key)
        return int(v) if v is not None and v != "" else None

    def _opt_float(key):
        v = body.get(key)
        return float(v) if v is not None and v != "" else None

    return await _control(
        name,
        lambda db, r: control.update_campaign_config(
            db, r, name,
            cron_cadence=body.get("cron_cadence") or None,
            model=body.get("model") or None,
            effort=body.get("effort") or None,
            max_daily_cost_usd=_opt_float("max_daily_cost_usd"),
            jitter_seconds=_opt_int("jitter_seconds"),
        ),
    )
