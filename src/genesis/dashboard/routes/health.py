"""Health snapshot, provider activity, and Guardian dialogue routes."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from flask import jsonify, request

from genesis.dashboard._blueprint import _async_route, blueprint

logger = logging.getLogger(__name__)

# Bound the health snapshot so a slow/contended snapshot returns 503 fast
# instead of hanging the Flask worker — which previously made the dashboard
# spin forever and failed the host Guardian's health probe. The snapshot is
# ~2s after the call_sites + gather fixes; 15s is a generous never-hang
# backstop, well under browser/proxy timeouts.
_HEALTH_SNAPSHOT_TIMEOUT_S = 15.0

# Snapshot cache — the full snapshot is ~2s; cache it briefly so frequent
# dashboard/Guardian polls are served instantly without recomputing every time.
# Defense-in-depth atop the call_sites + gather speedups: even if the snapshot
# ever regresses, the cache (plus the route timeout) keeps /health responsive
# and cuts repeated load. Bridge status and the healthy/unhealthy verdict are
# still computed fresh per request; only the expensive snapshot() is cached.
_snapshot_cache: dict | None = None
_snapshot_cache_ts: float = 0.0
_SNAPSHOT_CACHE_TTL_S = 30.0


_snapshot_cache_gen: int = 0


def invalidate_snapshot_cache() -> None:
    """Force the next /api/genesis/health to recompute.

    Call after any mutation to the queues/health data. Without this, a
    ``DELETE`` + immediate refetch is served from the <=30s-old cache, so the
    UI shows a success toast beside the pre-delete counts it just cleared
    (observed 2026-08-30: "Cleared 148" next to "showing 20 of 148").

    Three things are required, because there are two distinct staleness paths:
    clear the cached value; bump a generation counter so a compute that began
    BEFORE this call declines to publish when it lands after it; and FLAG the
    producer's in-flight computation so a caller arriving AFTER this call waits
    it out and recomputes rather than being handed that same pre-mutation
    result. Flagging (rather than dropping the handle) keeps the producer
    single-flight, which its probe-transition side effect requires.

    Called from a Flask request thread, so the generation counter is bumped
    concurrently with the loop thread reading it. The read-modify-write is not
    atomic and two simultaneous invalidations can collapse into one increment —
    which is harmless HERE, and deliberately left unlocked: the publish guard
    asks only whether the counter still equals the value sampled before the
    compute, and every racing writer stores ``load + 1``, so the counter always
    advances and can never return to a previously sampled value. It is a change
    detector, not a tally; nothing reads it as a count.
    """
    global _snapshot_cache, _snapshot_cache_ts, _snapshot_cache_gen
    _snapshot_cache = None
    _snapshot_cache_ts = 0.0
    _snapshot_cache_gen += 1

    # Clearing this module's cache is NOT enough: HealthDataService.snapshot()
    # coalesces overlapping callers onto one in-flight computation, so the next
    # request would be handed a result computed BEFORE the mutation — and,
    # having sampled the generation after the bump, would republish it with a
    # full fresh TTL. Because this function also nulls the cache, every request
    # in that window misses and coalesces onto that same stale compute, so
    # skipping this would widen the very window the call is meant to close.
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    health_data = getattr(rt, "health_data", None)
    if health_data is not None and hasattr(health_data, "mark_inflight_stale"):
        health_data.mark_inflight_stale()


@blueprint.route("/api/genesis/health")
@_async_route(timeout=_HEALTH_SNAPSHOT_TIMEOUT_S)
async def health_snapshot():
    """Return system health snapshot with bridge status from status.json."""
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.health_data is None:
        return jsonify({"status": "unhealthy", "error": "not bootstrapped"}), 503

    global _snapshot_cache, _snapshot_cache_ts
    now_mono = time.monotonic()
    if _snapshot_cache is not None and (now_mono - _snapshot_cache_ts) < _SNAPSHOT_CACHE_TTL_S:
        snapshot = dict(_snapshot_cache)
    else:
        gen = _snapshot_cache_gen
        fresh = await rt.health_data.snapshot()
        # Publish only if nothing invalidated the cache while we were computing;
        # otherwise this result predates a mutation and would resurrect it.
        if gen == _snapshot_cache_gen:
            _snapshot_cache = fresh
            _snapshot_cache_ts = time.monotonic()
        snapshot = dict(fresh)

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


@blueprint.route("/api/genesis/liveness")
def liveness_probe():
    """Off-loop liveness probe — answers even when the event loop is STARVED.

    Deliberately a SYNC route (NO ``@_async_route``): Flask serves it on its own
    worker thread (``threaded=True``), so it never bounces onto the runtime event
    loop and therefore still responds when background work has starved that loop —
    the exact case where ``/api/genesis/heartbeat`` (async) hangs indefinitely,
    indistinguishable from a dead process. Reads only plain in-memory values (the
    off-loop loop-health sample + the awareness loop's integer counters); never
    touches a coroutine, the event bus, or any loop-bound async object.

    Always HTTP 200 when the process + Flask thread are alive — classification
    (down / starved / wedged / responsive) is the CALLER's job. Fail-closed: any
    field it cannot read is ``null`` (UNKNOWN), never a healthy-looking default, so
    a consumer must treat ``loop: null`` as unknown rather than clear.
    """
    from genesis.util import loop_health

    loop_block = None
    sample = loop_health.read()
    if sample is not None:
        loop_block = {
            "lag_ms": round(sample.drift_ms, 1),
            "peak_ms": round(sample.peak_ms, 1),
            "lagging": sample.lagging,
            "threshold_ms": sample.threshold_ms,
            "executor": sample.executor,
            # Computed at READ time (see loop_health.age_s) — a growing age under
            # starvation is the WEDGED signal.
            "sample_age_s": round(loop_health.age_s(sample), 3),
        }

    awareness_block = None
    try:
        from genesis.runtime import GenesisRuntime

        # peek() — never lazily constructs a blank singleton (this is a read-only
        # observability path); None before bootstrap → awareness stays null.
        rt = GenesisRuntime.peek()
        if rt is not None and rt.is_bootstrapped and rt.awareness_loop is not None:
            awareness_block = {
                "tick_count": rt.awareness_loop.tick_count,
                "last_tick_at": rt.awareness_loop.last_tick_at,
            }
    except Exception:
        awareness_block = None

    return jsonify({
        "alive": True,
        "loop": loop_block,
        "awareness": awareness_block,
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

    # Dispatch Sentinel if available — container-side guardian handles it.
    # Guardian uses sentinel_state for event-driven standing (no wall-clock
    # timeout). As long as Sentinel is active, Guardian waits indefinitely.
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
            # Sentinel just dispatched — report its initial state
            s_state = getattr(sentinel, "_state", None)
            state_val = s_state.current_state if s_state else "investigating"
            return jsonify({
                "acknowledged": True,
                "status": "handling",
                "action": "sentinel_dispatched",
                "eta_s": 0,
                "sentinel_state": state_val,
                "context": f"Sentinel dispatched ({state_val})",
            }), 200
        except Exception:
            logger.warning("Sentinel dispatch failed — falling back to need_help", exc_info=True)
    elif sentinel is not None and sentinel.is_active:
        # Sentinel is already mid-remediation — tell Guardian to wait.
        # Without this, Guardian interprets the fallthrough as "need_help"
        # and escalates, causing competing restart attempts.
        s_state = getattr(sentinel, "_state", None)
        state_val = s_state.current_state if s_state else "investigating"
        return jsonify({
            "acknowledged": True,
            "status": "handling",
            "action": "sentinel_already_active",
            "eta_s": 0,
            "sentinel_state": state_val,
            "context": f"Sentinel active ({state_val})",
        }), 200

    return jsonify({
        "acknowledged": True,
        "status": "need_help",
        "action": "",
        "eta_s": 0,
        "context": "Genesis acknowledges the concern but cannot self-repair (Sentinel unavailable)",
    }), 200
