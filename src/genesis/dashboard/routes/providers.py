"""Provider detail and toggle routes."""

from __future__ import annotations

from flask import jsonify

from genesis.dashboard._blueprint import _async_route, blueprint, logger


@blueprint.route("/api/genesis/providers-detail")
@_async_route
async def providers_detail():
    """Return registered tool providers with health and usage info."""
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.provider_registry is None:
        return jsonify([])

    result = []
    for p in rt.provider_registry.list_all():
        cap = p.capability
        info = rt.provider_registry.info(p.name)
        result.append({
            "name": p.name,
            "categories": [str(c) for c in cap.categories],
            "cost_tier": str(cap.cost_tier),
            "description": cap.description,
            "content_types": list(cap.content_types),
            "status": str(info.status) if info else "unknown",
            "invocation_count": info.invocation_count if info else 0,
            "last_used": info.last_used if info else "",
        })
    return jsonify(result)


@blueprint.route("/api/genesis/providers/<name>/toggle", methods=["POST"])
def provider_toggle(name: str):
    """Toggle a provider on or off via its circuit breaker."""
    from genesis.routing.types import ProviderState
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped:
        return jsonify({"status": "error", "message": "runtime not available"}), 503

    breakers = rt.circuit_breakers
    if breakers is None:
        return jsonify({"status": "error", "message": "circuit breaker registry not available"}), 503

    try:
        cb = breakers.get(name)
    except KeyError:
        return jsonify({"status": "error", "message": f"provider '{name}' not found"}), 404

    if cb.state == ProviderState.OPEN:
        cb._state = ProviderState.CLOSED
        cb._consecutive_failures = 0
        cb._trip_count = 0
        breakers.save_state()
        logger.info("Provider '%s' re-enabled via dashboard (breaker reset to CLOSED)", name)
        return jsonify({"status": "ok", "name": name, "state": "closed", "enabled": True})
    else:
        cb._state = ProviderState.OPEN
        cb._opened_at = cb._clock()
        cb._trip_count = 99
        breakers.save_state()
        logger.info("Provider '%s' disabled via dashboard (breaker forced OPEN)", name)
        return jsonify({"status": "ok", "name": name, "state": "open", "enabled": False})
