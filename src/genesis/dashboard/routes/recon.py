"""Recon findings, code audit, and watchlist routes."""

from __future__ import annotations

import json
import logging

from flask import jsonify, request

from genesis.dashboard._blueprint import _async_route, blueprint
from genesis.dashboard.auth import is_authenticated

logger = logging.getLogger(__name__)


@blueprint.route("/api/genesis/recon/findings")
@_async_route
async def recon_findings():
    """Return recent code audit findings from observations."""
    from genesis.db.crud import observations as obs_crud
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.db is None:
        return jsonify([])

    limit = request.args.get("limit", 30, type=int)
    severity = request.args.get("severity")
    unresolved_only = request.args.get("unresolved_only", "false").lower() == "true"

    kwargs: dict = {
        "source": "recon",
        "category": "code_audit",
        "limit": min(limit, 100),
    }
    if unresolved_only:
        kwargs["resolved"] = False
    if severity:
        kwargs["priority"] = severity

    rows = await obs_crud.query(rt.db, **kwargs)

    results = []
    for row in rows:
        finding = dict(row)
        try:
            finding["parsed"] = json.loads(finding.get("content", "{}"))
        except (json.JSONDecodeError, TypeError):
            finding["parsed"] = {}
        results.append(finding)

    return jsonify(results)


# ── Tracked-repo watchlist (recon) ────────────────────────────────────
# The list (base + install overlay) is non-sensitive and readable; mutations
# write the gitignored overlay and are gated behind dashboard auth (a no-op
# when no password is configured). The recon MCP tool stays read-only by
# design, so the editor is the only write surface.

@blueprint.route("/api/genesis/recon/watchlist")
def recon_watchlist_list():
    """Annotated watchlist entries (base + overlay, with source/disabled)."""
    from genesis.recon import watchlist
    try:
        return jsonify({"entries": watchlist.list_entries()})
    except Exception:
        logger.error("Failed to list watchlist", exc_info=True)
        return jsonify({"entries": []})


@blueprint.route("/api/genesis/recon/watchlist", methods=["POST"])
def recon_watchlist_add():
    """Add an install-specific tracked repo to the overlay."""
    if not is_authenticated():
        return jsonify({"error": "authentication required"}), 401
    from genesis.recon import watchlist
    result = watchlist.add_repo(request.get_json(silent=True) or {})
    return (jsonify(result), 422) if "error" in result else jsonify(result)


@blueprint.route("/api/genesis/recon/watchlist/disable", methods=["POST"])
def recon_watchlist_disable():
    """Disable/re-enable a BASE watchlist entry (tombstone in the overlay)."""
    if not is_authenticated():
        return jsonify({"error": "authentication required"}), 401
    from genesis.recon import watchlist
    data = request.get_json(silent=True) or {}
    repo = str(data.get("repo") or "").strip()
    result = watchlist.set_base_disabled(repo, bool(data.get("disabled", True)))
    return (jsonify(result), 422) if "error" in result else jsonify(result)


@blueprint.route("/api/genesis/recon/watchlist", methods=["DELETE"])
def recon_watchlist_remove():
    """Remove an install-added repo from the overlay (base entries: disable)."""
    if not is_authenticated():
        return jsonify({"error": "authentication required"}), 401
    from genesis.recon import watchlist
    data = request.get_json(silent=True) or {}
    repo = str(data.get("repo") or "").strip()
    result = watchlist.remove_overlay_repo(repo)
    return (jsonify(result), 422) if "error" in result else jsonify(result)
