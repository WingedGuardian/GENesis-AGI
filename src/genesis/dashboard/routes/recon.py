"""Recon findings and code audit routes."""

from __future__ import annotations

import json

from flask import jsonify, request

from genesis.dashboard._blueprint import _async_route, blueprint


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


    # surplus_activity() moved to routes/surplus.py
