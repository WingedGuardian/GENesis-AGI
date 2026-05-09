"""Ego dashboard endpoints: cycles, proposals, cadence, follow-ups."""

from __future__ import annotations

from flask import jsonify, request

from genesis.dashboard._blueprint import _async_route, blueprint


@blueprint.route("/api/genesis/ego/status")
@_async_route
async def ego_status():
    """Return ego subsystem status: config, recent activity, daily cost."""
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
    import contextlib
    dispatch_cost = 0.0
    with contextlib.suppress(Exception):
        dispatch_cost = await ego_crud.daily_dispatch_cost(rt._db)

    return jsonify({
        "enabled": config.enabled,
        "model": config.model,
        "default_effort": config.default_effort,
        "morning_report_effort": config.morning_report_effort,
        "cadence_minutes": config.cadence_minutes,
        "ego_thinking_budget_usd": config.ego_thinking_budget_usd,
        "ego_dispatch_budget_usd": config.ego_dispatch_budget_usd,
        "daily_cost_usd": round(daily_cost, 4),
        "daily_dispatch_cost_usd": round(dispatch_cost, 4),
        "thinking_budget_remaining_usd": round(
            max(0, config.ego_thinking_budget_usd - daily_cost), 4,
        ),
        "dispatch_budget_remaining_usd": round(
            max(0, config.ego_dispatch_budget_usd - dispatch_cost), 4,
        ),
        # Backwards compat: total budget view
        "daily_budget_cap_usd": config.ego_thinking_budget_usd + config.ego_dispatch_budget_usd,
        "budget_remaining_usd": round(
            max(0, (config.ego_thinking_budget_usd - daily_cost)
                + (config.ego_dispatch_budget_usd - dispatch_cost)), 4,
        ),
        "focus_summary": focus,
        "last_cycle": last_cycle,
        "pending_proposals": len(pending),
        "uncompacted_cycles": uncompacted,
        "shadow_morning_report": config.shadow_morning_report,
        "board_size": config.board_size,
    })


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
        pass  # Non-critical

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
