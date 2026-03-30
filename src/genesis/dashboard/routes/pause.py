"""Pause/resume API endpoints for the Genesis kill switch."""

from __future__ import annotations

from flask import jsonify, request

from genesis.dashboard._blueprint import blueprint


@blueprint.route("/api/genesis/pause")
def get_pause_status():
    """Return current pause state."""
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    return jsonify({
        "paused": rt.paused,
        "reason": rt.pause_reason,
        "since": rt.paused_since.isoformat() if rt.paused_since else None,
    })


@blueprint.route("/api/genesis/pause", methods=["POST"])
def set_pause():
    """Set pause state. Body: {"paused": bool, "reason": str}."""
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    data = request.get_json(silent=True) or {}
    paused = data.get("paused", False)
    reason = data.get("reason", "Dashboard toggle")
    rt.set_paused(paused, reason)
    return jsonify({"ok": True, "paused": rt.paused})
