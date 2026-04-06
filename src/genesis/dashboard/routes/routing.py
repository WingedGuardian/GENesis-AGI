"""Routing config editor routes."""

from __future__ import annotations

import logging
from pathlib import Path

import yaml
from flask import jsonify, request

from genesis.dashboard._blueprint import blueprint
from genesis.observability._call_site_meta import _CALL_SITE_META

logger = logging.getLogger(__name__)


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

    # Read raw YAML for CC fields that may have been edited via the dashboard
    yaml_cc: dict[str, dict] = {}
    config_path = Path(__file__).parent.parent.parent.parent.parent / "config" / "model_routing.yaml"
    if config_path.exists():
        try:
            raw = yaml.safe_load(config_path.read_text()) or {}
            for cs_name, cs_raw in (raw.get("call_sites") or {}).items():
                if isinstance(cs_raw, dict) and (cs_raw.get("dispatch") or cs_raw.get("cc_model")):
                    yaml_cc[cs_name] = cs_raw
        except Exception:
            logger.debug("Failed to read CC overrides from YAML", exc_info=True)

    call_sites = {}
    for name, cs in cfg.call_sites.items():
        meta = _CALL_SITE_META.get(name)
        site_data = {
            "chain": list(cs.chain),
            "default_paid": cs.default_paid,
            "never_pays": cs.never_pays,
            "retry_profile": cs.retry_profile,
        }
        # CC info: YAML overrides meta (YAML is updated by saves)
        yaml_entry = yaml_cc.get(name, {})
        dispatch = yaml_entry.get("dispatch") or (meta.get("dispatch") if meta else None)
        cc_model = yaml_entry.get("cc_model") or (meta.get("cc_model") if meta else None)
        cc_position = yaml_entry.get("cc_position")
        if dispatch:
            site_data["dispatch"] = dispatch
            site_data["cc_model"] = cc_model
        if cc_position is not None:
            site_data["cc_position"] = cc_position
        call_sites[name] = site_data

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
    cc_model = data.get("cc_model")  # str or None
    cc_position = data.get("cc_position")  # int or None

    config_path = Path(__file__).parent.parent.parent.parent.parent / "config" / "model_routing.yaml"
    if not config_path.exists():
        return jsonify({"error": "model_routing.yaml not found"}), 404

    try:
        new_config = update_call_site_in_yaml(
            config_path, call_site_id,
            chain=chain, default_paid=default_paid, never_pays=never_pays,
            cc_model=cc_model, cc_position=cc_position,
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
