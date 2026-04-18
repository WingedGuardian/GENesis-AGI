"""Dashboard settings routes — read/write config domains via the MCP settings backend."""

from __future__ import annotations

import logging

from flask import jsonify, request

from genesis.dashboard._blueprint import _async_route, blueprint
from genesis.mcp.health.settings import (
    _DOMAIN_REGISTRY,
    _DOMAIN_VALIDATORS,
    _atomic_yaml_write,
    _deep_merge,
    _load_yaml,
    _load_yaml_local,
    _load_yaml_merged,
    _local_filename,
)

logger = logging.getLogger(__name__)


@blueprint.route("/api/genesis/settings", methods=["GET"])
@_async_route
async def settings_index():
    """List all settings domains with metadata."""
    domains = []
    for domain in _DOMAIN_REGISTRY.values():
        domains.append({
            "name": domain.name,
            "description": domain.description,
            "readonly": domain.readonly,
            "readonly_reason": domain.readonly_reason,
            "needs_restart": domain.needs_restart,
            "dedicated_tool": domain.dedicated_tool,
            "has_form": domain.name in _FORM_DOMAINS,
        })
    return jsonify(domains)


@blueprint.route("/api/genesis/settings/<domain_name>", methods=["GET"])
@_async_route
async def settings_get(domain_name: str):
    """Read a settings domain's current values."""
    domain = _DOMAIN_REGISTRY.get(domain_name)
    if not domain:
        return jsonify({"error": f"Unknown domain: {domain_name}"}), 404
    data = _load_yaml_merged(domain.config_filename)
    return jsonify({"domain": domain_name, "config": data, "readonly": domain.readonly})


@blueprint.route("/api/genesis/settings/<domain_name>", methods=["PUT"])
@_async_route
async def settings_update(domain_name: str):
    """Update a settings domain. Validates before writing."""
    domain = _DOMAIN_REGISTRY.get(domain_name)
    if not domain:
        return jsonify({"error": f"Unknown domain: {domain_name}"}), 404
    if domain.readonly:
        return jsonify({"error": f"Domain '{domain_name}' is read-only"}), 403

    changes = request.get_json(silent=True)
    if not changes or not isinstance(changes, dict):
        return jsonify({"error": "Request body must be a JSON object"}), 400

    # Validate
    validator = _DOMAIN_VALIDATORS.get(domain_name)
    if validator:
        errors = validator(changes)
        if errors:
            return jsonify({"error": "Validation failed", "details": errors}), 422

    # Write changes to the local overlay (not the base file)
    try:
        local = _load_yaml_local(domain.config_filename)
        new_local = _deep_merge(local, changes)
        local_file = _local_filename(domain.config_filename)
        _atomic_yaml_write(local_file, new_local)
        logger.info("Settings domain '%s' updated via dashboard (local overlay)", domain_name)
        # Return the full merged view
        base = _load_yaml(domain.config_filename)
        merged = _deep_merge(base, new_local)
        return jsonify({
            "domain": domain_name,
            "config": merged,
            "needs_restart": domain.needs_restart,
        })
    except Exception:
        logger.error("Failed to update settings domain '%s'", domain_name, exc_info=True)
        return jsonify({"error": "Failed to write settings"}), 500


# Domains that get dedicated form UI on the dashboard
_FORM_DOMAINS = frozenset({
    "tts", "ego", "inbox_monitor", "outreach", "autonomous_cli_policy",
})
