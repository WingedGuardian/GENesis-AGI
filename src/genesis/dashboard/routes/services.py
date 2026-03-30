"""Service restart and MCP tools routes."""

from __future__ import annotations

import asyncio as _aio
import importlib
import subprocess

from flask import jsonify

from genesis.dashboard._blueprint import _async_route, blueprint


@blueprint.route("/api/genesis/restart/bridge", methods=["POST"])
def restart_bridge():
    """Restart the Genesis bridge via systemd. Idempotent, never duplicates."""
    try:
        result = subprocess.run(
            ["systemctl", "--user", "restart", "genesis-bridge.service"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            return jsonify({"status": "ok", "message": "Bridge restart initiated"})
        return jsonify({"status": "error", "message": result.stderr.strip()}), 500
    except subprocess.TimeoutExpired:
        return jsonify({"status": "error", "message": "Restart timed out"}), 500
    except Exception as exc:
        return jsonify({"status": "error", "message": str(exc)}), 500


@blueprint.route("/api/genesis/restart/agent-zero", methods=["POST"])
def restart_agent_zero():
    """Restart Agent Zero via systemd. Dashboard will reload after restart."""
    try:
        result = subprocess.run(
            ["systemctl", "--user", "restart", "agent-zero.service"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            return jsonify({"status": "ok", "message": "Agent Zero restart initiated"})
        return jsonify({"status": "error", "message": result.stderr.strip()}), 500
    except subprocess.TimeoutExpired:
        return jsonify({"status": "error", "message": "Restart timed out"}), 500
    except Exception as exc:
        return jsonify({"status": "error", "message": str(exc)}), 500


# Allowed restart commands — only systemctl restarts of known service patterns.
_ALLOWED_RESTART_PREFIXES = ("systemctl --user restart ",)


@blueprint.route("/api/genesis/restart/host-framework", methods=["POST"])
def restart_host_framework():
    """Restart the detected host framework using its cached restart command.

    Generic endpoint — works for any detected host framework without hardcoding.
    The restart command is validated against an allowlist before execution.
    """
    try:
        from genesis.observability.snapshots.services import _get_registry

        status = _get_registry().detect()
        if not status.detected or not status.restart_cmd:
            return jsonify({
                "status": "error",
                "message": "No host framework detected or no restart command available",
            }), 404

        # Validate command against allowlist
        if not any(status.restart_cmd.startswith(p) for p in _ALLOWED_RESTART_PREFIXES):
            return jsonify({
                "status": "error",
                "message": "Restart command not in allowlist",
            }), 403

        cmd_parts = status.restart_cmd.split()
        result = subprocess.run(
            cmd_parts,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            return jsonify({
                "status": "ok",
                "message": f"{status.name} restart initiated",
            })
        return jsonify({"status": "error", "message": result.stderr.strip()}), 500
    except subprocess.TimeoutExpired:
        return jsonify({"status": "error", "message": "Restart timed out"}), 500
    except Exception as exc:
        return jsonify({"status": "error", "message": str(exc)}), 500


@blueprint.route("/api/genesis/mcp/tools")
@_async_route
async def mcp_tools():
    """Return tool names per MCP server."""
    mcp_modules = [
        ("health", "genesis.mcp.health_mcp"),
        ("memory", "genesis.mcp.memory_mcp"),
        ("outreach", "genesis.mcp.outreach_mcp"),
        ("recon", "genesis.mcp.recon_mcp"),
    ]
    result = {}
    for name, module_path in mcp_modules:
        try:
            mod = importlib.import_module(module_path)
            mcp_obj = getattr(mod, "mcp", None)
            if mcp_obj is None:
                result[name] = {"status": "error", "tools": []}
                continue
            tools = await _aio.wait_for(mcp_obj.get_tools(), timeout=2.0)
            result[name] = {
                "status": "up",
                "tools": sorted(tools.keys()) if isinstance(tools, dict) else sorted(str(t) for t in tools),
            }
        except Exception as exc:
            result[name] = {"status": "error", "error": str(exc), "tools": []}
    return jsonify(result)
