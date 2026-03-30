"""Routing config editor routes."""

from __future__ import annotations

from pathlib import Path

from flask import jsonify, request

from genesis.dashboard._blueprint import blueprint


@blueprint.route("/api/genesis/routing/config")
def routing_config_read():
    """Return current routing config as JSON for the editor."""
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.router is None:
        return jsonify({"error": "not bootstrapped"}), 503

    cfg = rt.router.config
    providers = {
        name: {
            "name": p.name,
            "type": p.provider_type,
            "model": p.model_id,
            "free": p.is_free,
        }
        for name, p in cfg.providers.items()
    }

    cb_states = {}
    for name in cfg.providers:
        cb = rt.router.breakers.get(name)
        cb_states[name] = cb.state.value if hasattr(cb, "state") else "closed"

    call_sites = {
        name: {
            "chain": list(cs.chain),
            "default_paid": cs.default_paid,
            "never_pays": cs.never_pays,
            "retry_profile": cs.retry_profile,
        }
        for name, cs in cfg.call_sites.items()
    }

    return jsonify({
        "providers": providers,
        "cb_states": cb_states,
        "call_sites": call_sites,
    })


@blueprint.route("/api/genesis/routing/config/<call_site_id>", methods=["PUT"])
def routing_config_update(call_site_id: str):
    """Update a single call site's chain/policy and reload config."""
    from genesis.routing.config import update_call_site_in_yaml
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.router is None:
        return jsonify({"error": "not bootstrapped"}), 503

    data = request.get_json(silent=True) or {}
    chain = data.get("chain")
    default_paid = data.get("default_paid")
    never_pays = data.get("never_pays")

    config_path = Path(__file__).parent.parent.parent.parent.parent / "config" / "model_routing.yaml"
    if not config_path.exists():
        return jsonify({"error": "model_routing.yaml not found"}), 404

    try:
        new_config = update_call_site_in_yaml(
            config_path, call_site_id,
            chain=chain, default_paid=default_paid, never_pays=never_pays,
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": f"Config update failed: {type(e).__name__}"}), 500

    rt.router.reload_config(new_config)

    return jsonify({"ok": True, "call_site_id": call_site_id})


@blueprint.route("/api/genesis/routing/reload", methods=["POST"])
def routing_config_reload():
    """Re-read the YAML config from disk and reload the router."""
    from genesis.routing.config import load_config
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.router is None:
        return jsonify({"error": "not bootstrapped"}), 503

    config_path = Path(__file__).parent.parent.parent.parent.parent / "config" / "model_routing.yaml"
    if not config_path.exists():
        return jsonify({"error": "model_routing.yaml not found"}), 404

    try:
        new_config = load_config(config_path)
    except Exception as e:
        return jsonify({"error": f"Config parse failed: {e}"}), 400

    rt.router.reload_config(new_config)
    return jsonify({"ok": True})
