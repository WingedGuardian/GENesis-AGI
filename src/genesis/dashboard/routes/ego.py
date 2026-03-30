"""Ego dashboard endpoints: cycles, proposals, statistics."""

from __future__ import annotations

from flask import jsonify

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

    return jsonify({
        "enabled": config.enabled,
        "model": config.model,
        "cadence_minutes": config.cadence_minutes,
        "daily_budget_cap_usd": config.daily_budget_cap_usd,
        "daily_cost_usd": round(daily_cost, 4),
        "budget_remaining_usd": round(
            max(0, config.daily_budget_cap_usd - daily_cost), 4,
        ),
        "focus_summary": focus,
        "last_cycle": last_cycle,
        "pending_proposals": len(pending),
        "uncompacted_cycles": uncompacted,
        "shadow_morning_report": config.shadow_morning_report,
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
        }
        for p in pending
    ])
