"""Ego dashboard endpoints: cycles, proposals, cadence, follow-ups."""

from __future__ import annotations

from flask import jsonify, request

from genesis.dashboard._blueprint import _async_route, blueprint


@blueprint.route("/api/genesis/ego/status")
@_async_route
async def ego_status():
    """Return ego subsystem status: config, recent activity, daily cost.

    Includes per-ego breakdown (user ego + genesis ego) for dual-ego
    dashboard display.
    """
    import contextlib

    from genesis.db.crud import ego as ego_crud
    from genesis.ego.config import load_ego_config
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt._db is None:
        return jsonify({"status": "not_bootstrapped"})

    config = load_ego_config()
    daily_cost = await ego_crud.daily_ego_cost(rt._db)
    focus = await ego_crud.get_state(rt._db, "ego_focus_summary")
    recent = await ego_crud.list_recent_cycles(rt._db, limit=1)
    pending = await ego_crud.list_pending_proposals(rt._db)
    uncompacted = await ego_crud.count_uncompacted(rt._db)

    last_cycle = None
    if recent:
        c = recent[0]
        last_cycle = {
            "id": c["id"],
            "created_at": c["created_at"],
            "focus_summary": c["focus_summary"],
            "cost_usd": c["cost_usd"],
            "model_used": c["model_used"],
        }

    # Dispatch cost from cc_sessions
    dispatch_cost = 0.0
    with contextlib.suppress(Exception):
        dispatch_cost = await ego_crud.daily_dispatch_cost(rt._db)

    # Rolling 7-day average (observational only — no gating)
    rolling_avg = 0.0
    with contextlib.suppress(Exception):
        rolling_avg = await ego_crud.rolling_daily_ego_cost(rt._db, days=7)

    # ── Per-ego breakdown ──────────────────────────────────────────────
    # Split pending proposals by ego_source for per-ego counts
    user_pending = [p for p in pending if p.get("ego_source") == "user_ego_cycle"]
    genesis_pending = [p for p in pending if p.get("ego_source") == "genesis_ego_cycle"]

    # Genesis ego focus summary (separate state key)
    genesis_focus = await ego_crud.get_state(rt._db, "genesis_ego_focus_summary")

    # Cadence state from runtime managers
    def _cadence_snapshot(mgr) -> dict:
        if mgr is None:
            return {"available": False}
        return {
            "available": True,
            "is_running": mgr.is_running,
            "is_paused": mgr.is_paused,
            "current_interval_minutes": mgr.current_interval_minutes,
            "consecutive_failures": mgr.consecutive_failures,
        }

    user_cadence = _cadence_snapshot(rt._ego_cadence_manager)
    genesis_cadence = _cadence_snapshot(rt._genesis_ego_cadence_manager)

    # Genesis ego config — uses dedicated config fields
    genesis_ego_config = {
        "model": "sonnet",
        "default_effort": "high",
        "cadence_minutes": config.genesis_cadence_minutes,
        "max_interval_minutes": config.genesis_max_interval_minutes,
        "morning_report_enabled": False,
    }

    egos = {
        "user_ego": {
            "label": "User Ego",
            "role": "CEO",
            "model": config.model,
            "effort": config.default_effort,
            "focus_summary": focus or "no focus",
            "pending_proposals": len(user_pending),
            "cadence": user_cadence,
            "cadence_minutes": config.cadence_minutes,
            "max_interval_minutes": config.max_interval_minutes,
            "morning_report": config.morning_report_enabled,
        },
        "genesis_ego": {
            "label": "Genesis Ego",
            "role": "COO",
            "model": genesis_ego_config["model"],
            "effort": genesis_ego_config["default_effort"],
            "focus_summary": genesis_focus or "no focus",
            "pending_proposals": len(genesis_pending),
            "cadence": genesis_cadence,
            "cadence_minutes": genesis_ego_config["cadence_minutes"],
            "max_interval_minutes": genesis_ego_config["max_interval_minutes"],
            "morning_report": False,
        },
    }

    return jsonify({
        "enabled": config.enabled,
        "model": config.model,
        "default_effort": config.default_effort,
        "morning_report_effort": config.morning_report_effort,
        "cadence_minutes": config.cadence_minutes,
        "daily_cost_usd": round(daily_cost, 4),
        "daily_dispatch_cost_usd": round(dispatch_cost, 4),
        "rolling_daily_avg_usd": round(rolling_avg, 4),
        "focus_summary": focus,
        "last_cycle": last_cycle,
        "pending_proposals": len(pending),
        "uncompacted_cycles": uncompacted,
        "shadow_morning_report": config.shadow_morning_report,
        "board_size": config.board_size,
        "egos": egos,
    })


@blueprint.route("/api/genesis/ego/trigger", methods=["POST"])
@_async_route
async def ego_trigger():
    """Manually trigger a user ego cycle.

    Invokes the cadence manager's _on_tick() which respects the
    asyncio lock (no concurrent cycles) and circuit breaker state.
    Returns the cycle result or an error.
    """
    import logging

    from genesis.runtime import GenesisRuntime

    logger = logging.getLogger(__name__)
    rt = GenesisRuntime.instance()

    if not rt.is_bootstrapped:
        return jsonify({"error": "not bootstrapped"}), 503

    mgr = rt._ego_cadence_manager
    if mgr is None:
        return jsonify({"error": "ego cadence manager not available"}), 503

    target = request.args.get("ego", "user")
    if target == "genesis":
        mgr = rt._genesis_ego_cadence_manager
        if mgr is None:
            return jsonify({"error": "genesis ego cadence manager not available"}), 503

    try:
        await mgr._on_tick()
        return jsonify({"status": "ok", "message": f"{target} ego cycle triggered"})
    except Exception:
        logger.error("Manual ego trigger failed", exc_info=True)
        return jsonify({"error": "Ego trigger failed — check server logs"}), 500


@blueprint.route("/api/genesis/ego/cycles")
@_async_route
async def ego_cycles():
    """Return recent ego cycles."""
    from genesis.db.crud import ego as ego_crud
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt._db is None:
        return jsonify([])

    cycles = await ego_crud.list_recent_cycles(rt._db, limit=20)
    return jsonify([
        {
            "id": c["id"],
            "created_at": c["created_at"],
            "focus_summary": c["focus_summary"],
            "cost_usd": c["cost_usd"],
            "model_used": c["model_used"],
            "input_tokens": c["input_tokens"],
            "output_tokens": c["output_tokens"],
            "duration_ms": c["duration_ms"],
            "compacted_into": c["compacted_into"],
            "ego_source": c.get("ego_source", ""),
        }
        for c in cycles
    ])


@blueprint.route("/api/genesis/ego/proposals")
@_async_route
async def ego_proposals():
    """Return pending ego proposals."""
    from genesis.db.crud import ego as ego_crud
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt._db is None:
        return jsonify([])

    pending = await ego_crud.list_pending_proposals(rt._db)
    return jsonify([
        {
            "id": p["id"],
            "action_type": p["action_type"],
            "action_category": p["action_category"],
            "content": p["content"],
            "confidence": p["confidence"],
            "urgency": p["urgency"],
            "status": p["status"],
            "created_at": p["created_at"],
            "expires_at": p["expires_at"],
            "rank": p.get("rank"),
            "execution_plan": p.get("execution_plan"),
            "recurring": bool(p.get("recurring", 0)),
            "ego_source": p.get("ego_source"),
            "realist_verdict": p.get("realist_verdict"),
        }
        for p in pending
    ])


@blueprint.route("/api/genesis/ego/cadence")
@_async_route
async def ego_cadence():
    """Return runtime cadence state from the EgoCadenceManager."""
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped:
        return jsonify({"available": False})

    mgr = rt._ego_cadence_manager
    if mgr is None:
        return jsonify({"available": False})

    return jsonify({
        "available": True,
        "is_running": mgr.is_running,
        "is_paused": mgr.is_paused,
        "current_interval_minutes": mgr.current_interval_minutes,
        "consecutive_failures": mgr.consecutive_failures,
    })


@blueprint.route("/api/genesis/ego/proposals/all")
@_async_route
async def ego_proposals_all():
    """Return all proposals with optional status filter and limit."""
    from genesis.db.crud import ego as ego_crud
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt._db is None:
        return jsonify([])

    status = request.args.get("status") or None
    limit = min(request.args.get("limit", 50, type=int), 200)

    proposals = await ego_crud.list_proposals(rt._db, status=status, limit=limit)
    return jsonify([
        {
            "id": p["id"],
            "action_type": p["action_type"],
            "action_category": p["action_category"],
            "content": p["content"],
            "rationale": p["rationale"],
            "confidence": p["confidence"],
            "urgency": p["urgency"],
            "alternatives": p["alternatives"],
            "status": p["status"],
            "user_response": p["user_response"],
            "cycle_id": p["cycle_id"],
            "batch_id": p["batch_id"],
            "created_at": p["created_at"],
            "resolved_at": p["resolved_at"],
            "expires_at": p["expires_at"],
            "rank": p.get("rank"),
            "execution_plan": p.get("execution_plan"),
            "recurring": bool(p.get("recurring", 0)),
            "ego_source": p.get("ego_source"),
        }
        for p in proposals
    ])


@blueprint.route("/api/genesis/ego/proposals/<proposal_id>/resolve", methods=["POST"])
@_async_route
async def ego_proposal_resolve(proposal_id: str):
    """Approve or reject a proposal from the dashboard."""
    from genesis.db.crud import ego as ego_crud
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt._db is None:
        return jsonify({"ok": False, "error": "not bootstrapped"}), 503

    body = request.get_json(silent=True) or {}
    status = body.get("status")
    if status not in ("approved", "rejected"):
        return jsonify({"ok": False, "error": "status must be 'approved' or 'rejected'"}), 400

    user_response = body.get("response", "")
    updated = await ego_crud.resolve_proposal(
        rt._db, proposal_id, status=status, user_response=user_response,
    )
    if not updated:
        return jsonify({"ok": False, "error": "proposal not found or not pending"}), 404

    try:
        from genesis.db.crud import intervention_journal as journal_crud
        await journal_crud.resolve(
            rt._db, proposal_id,
            outcome_status=status,
            actual_outcome=f"Dashboard: user {status}",
            user_response=user_response or None,
        )
    except Exception:
        import logging
        logging.getLogger(__name__).warning("Journal resolve failed for %s", proposal_id)

    # Autonomy earn-back: promote on approval / cooldown on reject.
    try:
        from genesis.ego.earnback import handle_earnback_resolution

        prop = await ego_crud.get_proposal(rt._db, proposal_id)
        if prop:
            await handle_earnback_resolution(
                rt._db, prop, status, getattr(rt, "_autonomy_manager", None),
            )
    except Exception:
        import logging
        logging.getLogger(__name__).warning(
            "earnback resolution hook failed for %s", proposal_id,
        )

    # Goal status change: apply pause/deprioritize on approval.
    try:
        from genesis.ego.goal_actions import handle_goal_status_change_resolution

        prop = await ego_crud.get_proposal(rt._db, proposal_id)
        if prop:
            await handle_goal_status_change_resolution(rt._db, prop, status)
    except Exception:
        import logging
        logging.getLogger(__name__).warning(
            "goal status-change hook failed for %s", proposal_id,
        )

    return jsonify({"ok": True, "id": proposal_id, "status": status})


@blueprint.route("/api/genesis/ego/follow-ups")
@_async_route
async def ego_follow_ups():
    """Return ego-originated follow-up items."""
    from genesis.db.crud import follow_ups as fu_crud
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt._db is None:
        return jsonify([])

    items = await fu_crud.get_pending(rt._db, source="ego_cycle")
    return jsonify([
        {
            "id": f["id"],
            "content": f["content"],
            "reason": f["reason"],
            "strategy": f["strategy"],
            "status": f["status"],
            "priority": f["priority"],
            "created_at": f["created_at"],
            "scheduled_at": f["scheduled_at"],
        }
        for f in items
    ])


@blueprint.route("/api/genesis/ego/vcr")
@_async_route
async def ego_vcr():
    """Return Verified Completion Rate for ego proposals."""
    from genesis.db.crud.ego import compute_vcr
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt._db is None:
        return jsonify({
            "vcr": 0.0, "dispatch_rate": 0.0,
            "total_resolved": 0, "total_executed": 0,
            "outcomes_completed": 0, "outcomes_failed": 0,
            "outcomes_unknown": 0,
        })

    data = await compute_vcr(rt._db, days=30)
    return jsonify(data)
