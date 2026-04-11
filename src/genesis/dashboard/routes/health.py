"""Health snapshot, provider activity, and Guardian dialogue routes."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from flask import jsonify, request

from genesis.dashboard._blueprint import _async_route, blueprint

logger = logging.getLogger(__name__)


@blueprint.route("/api/genesis/health")
@_async_route
async def health_snapshot():
    """Return system health snapshot with bridge status from status.json."""
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.health_data is None:
        return jsonify({"status": "unhealthy", "error": "not bootstrapped"}), 503

    snapshot = await rt.health_data.snapshot()

    status_path = Path.home() / ".genesis" / "status.json"
    bridge_health = None
    try:
        raw = status_path.read_text()
        bridge_health = json.loads(raw)
    except (json.JSONDecodeError, OSError):
        pass

    snapshot["bridge"] = bridge_health

    infra = snapshot.get("infrastructure", {})
    db_status = infra.get("genesis.db", {}).get("status", "") if isinstance(infra, dict) else ""
    healthy = rt.is_bootstrapped and db_status == "healthy"

    snapshot["status"] = "healthy" if healthy else "unhealthy"
    status_code = 200 if healthy else 503

    return jsonify(snapshot), status_code


@blueprint.route("/api/genesis/heartbeat")
@_async_route
async def heartbeat_canary():
    """Heartbeat canary for the Guardian — confirms awareness loop is alive."""
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped:
        return jsonify({"alive": False, "reason": "not bootstrapped"}), 503

    tick_count = 0
    last_tick_at = None
    if rt.awareness_loop is not None:
        tick_count = rt.awareness_loop.tick_count
        last_tick_at = rt.awareness_loop.last_tick_at

    return jsonify({
        "alive": True,
        "tick_count": tick_count,
        "last_tick_at": last_tick_at,
    }), 200


@blueprint.route("/api/genesis/provider-activity")
@_async_route
async def provider_activity():
    """Return per-provider call stats from the activity tracker."""
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.activity_tracker is None:
        return jsonify([])

    provider_name = request.args.get("provider")
    if provider_name:
        result = rt.activity_tracker.summary(provider_name)
        if isinstance(result, dict):
            return jsonify([result])
        return jsonify(result)

    result = await rt.activity_tracker.summary_with_db_fallback()
    return jsonify(result)


# GROUNDWORK(guardian-dialogue): Self-heal protocol endpoint.
# V4 Step 1: acknowledge concern + respond need_help (no self-healing yet).
# V4.5+: Genesis inspects its own state and attempts self-repair.
@blueprint.route("/api/genesis/guardian-dialogue", methods=["POST"])
@_async_route
async def guardian_dialogue():
    """Receive a health concern from the Guardian and respond.

    Protocol: Guardian sends failing signals, Genesis responds with
    one of: handling, need_help, stand_down.
    """
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()

    if not rt.is_bootstrapped:
        return jsonify({
            "acknowledged": False,
            "status": "need_help",
            "action": "",
            "eta_s": 0,
            "context": "Genesis is not bootstrapped",
        }), 503

    # Check if Genesis is paused — Guardian should stand down
    if rt.paused:
        pause_reason = ""
        try:
            pause_path = Path.home() / ".genesis" / "paused.json"
            if pause_path.exists():
                data = json.loads(pause_path.read_text())
                pause_reason = data.get("reason", "")
        except (json.JSONDecodeError, OSError):
            pass

        return jsonify({
            "acknowledged": True,
            "status": "stand_down",
            "action": "paused",
            "eta_s": 0,
            "context": f"Genesis is paused: {pause_reason}" if pause_reason else "Genesis is paused",
        }), 200

    # Log the concern for observability
    try:
        concern = request.get_json(silent=True) or {}
        failing = concern.get("signals_failing", [])
        logger.warning(
            "Guardian health concern received: signals_failing=%s, duration_s=%s",
            failing, concern.get("duration_s"),
        )
    except (ValueError, TypeError, AttributeError) as exc:
        logger.debug("Failed to parse Guardian concern payload: %s", exc, exc_info=True)

    # Dispatch Sentinel if available — container-side guardian handles it
    sentinel = getattr(rt, "_sentinel", None)
    if sentinel is not None and not sentinel.is_active:
        try:
            from genesis.sentinel import SentinelRequest
            from genesis.util.tasks import tracked_task

            tracked_task(
                sentinel.dispatch(SentinelRequest(
                    trigger_source="guardian_dialogue",
                    trigger_reason=f"Guardian concern: signals_failing={failing}",
                    tier=2,
                    context=concern,
                )),
                name="sentinel-guardian-dialogue",
            )
            return jsonify({
                "acknowledged": True,
                "status": "handling",
                "action": "sentinel_dispatched",
                "eta_s": 300,
                "context": "Sentinel dispatched to diagnose and fix",
            }), 200
        except Exception:
            logger.warning("Sentinel dispatch failed — falling back to need_help", exc_info=True)

    return jsonify({
        "acknowledged": True,
        "status": "need_help",
        "action": "",
        "eta_s": 0,
        "context": "Genesis acknowledges the concern but cannot self-repair (Sentinel unavailable)",
    }), 200
