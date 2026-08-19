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
    gate_disable_error,
)

logger = logging.getLogger(__name__)


async def _notify_gate_disabled() -> None:
    """Owner Telegram notice for a confirmed approval-gate disable.

    Best-effort — a notification failure never fails the settings write
    (the provenance stamp + warning log remain the durable record). Owner-
    facing delivery is never egress-gated.
    """
    try:
        from genesis.outreach.types import OutreachCategory, OutreachRequest
        from genesis.runtime import GenesisRuntime

        rt = GenesisRuntime.instance()
        pipeline = getattr(rt, "outreach_pipeline", None)
        if not rt.is_bootstrapped or pipeline is None:
            logger.warning(
                "Approval gate disabled but outreach pipeline unavailable — "
                "no Telegram notice sent",
            )
            return
        result = await pipeline.submit(
            OutreachRequest(
                category=OutreachCategory.ALERT,
                topic="Approval gate disabled",
                context=(
                    "manual_approval_required was set to FALSE via the "
                    "dashboard (confirmed). Autonomous Claude Code sessions "
                    "now dispatch WITHOUT per-run approval until it is set "
                    "back to true (Settings → autonomous_cli_policy)."
                ),
                salience_score=1.0,
                signal_type="approval_gate_disabled",
                channel="telegram",
                verbatim=True,
            ),
        )
        status = getattr(result, "status", None)
        delivered = getattr(status, "value", status)
        # Real OutreachStatus values: delivered/engaged are success; pending/
        # drafted are in-flight (not failures); rejected/failed/unknown warn.
        if str(delivered).lower() in ("rejected", "failed") or delivered is None:
            logger.warning(
                "Gate-disable Telegram notice not delivered (status=%s)",
                delivered,
            )
    except Exception:
        logger.warning("Gate-disable Telegram notice failed", exc_info=True)


def _strip_hidden(domain, data: dict) -> dict:
    """Remove hidden_fields from a config dict before serving to UI."""
    for field in domain.hidden_fields:
        data.pop(field, None)
        # Also strip inside wrapper keys (e.g., {inbox_monitor: {timezone: ...}})
        wrapper = data.get(domain.name)
        if isinstance(wrapper, dict):
            wrapper.pop(field, None)
    return data


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
    if domain_name == "reflection_models":
        # Expose an effort control only for depths whose model supports effort
        # (Haiku shows model only; switching a depth to Sonnet/Opus/Fable surfaces
        # effort). Pure view transform — the stored config is untouched.
        from genesis.cc.reflection_bridge.reflection_models_config import editor_view

        data = editor_view(data)
    _strip_hidden(domain, data)
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

    # Out-of-band confirmation flag — never merged into the config itself.
    # Strict identity check: bool("false") is True — a client serializing
    # booleans as strings must NEVER accidentally satisfy the confirmation.
    confirm_gate = changes.pop("confirm_disable_approval_gate", None) is True

    # Validate
    validator = _DOMAIN_VALIDATORS.get(domain_name)
    if validator:
        errors = validator(changes)
        if errors:
            return jsonify({"error": "Validation failed", "details": errors}), 422

    # Protected key: disabling the mandatory approval gate requires explicit
    # confirmation (2026-08-18: a single unconfirmed PUT used to flip it
    # silently), and a confirmed disable is announced via Telegram below.
    gate_err = gate_disable_error(domain_name, changes, confirmed=confirm_gate)
    if gate_err:
        return jsonify({
            "error": "confirmation required",
            "details": gate_err,
        }), 409

    # Write changes to the local overlay (not the base file)
    try:
        local = _load_yaml_local(domain.config_filename)
        new_local = _deep_merge(local, changes)
        local_file = _local_filename(domain.config_filename)
        _atomic_yaml_write(
            local_file, new_local, provenance="user via dashboard PUT",
        )
        logger.info("Settings domain '%s' updated via dashboard (local overlay)", domain_name)
        if (
            domain_name == "autonomous_cli_policy"
            and changes.get("manual_approval_required") is False
        ):
            await _notify_gate_disabled()
        elif (
            domain_name == "autonomous_cli_policy"
            and changes.get("manual_approval_required") is True
        ):
            from genesis.mcp.health.settings import resolve_gate_disable_alert

            await resolve_gate_disable_alert("user via dashboard PUT")
        # Return the full merged view
        base = _load_yaml(domain.config_filename)
        merged = _deep_merge(base, new_local)
        _strip_hidden(domain, merged)
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
    "surplus", "resilience", "confidence_gates", "updates", "channels",
    "reflection_models", "contribution",
})
