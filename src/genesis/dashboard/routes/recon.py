"""Recon findings and code audit routes."""

from __future__ import annotations

import json

import aiosqlite
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


@blueprint.route("/api/genesis/surplus/activity")
@_async_route
async def surplus_activity():
    """Return surplus activity summary including code audit stats."""
    from genesis.db.crud import observations as obs_crud
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.db is None:
        return jsonify({"error": "not bootstrapped"}), 503

    rt.db.row_factory = aiosqlite.Row

    cursor = await rt.db.execute(
        """SELECT created_at, status FROM surplus_tasks
           WHERE task_type = 'code_audit'
           ORDER BY created_at DESC LIMIT 1"""
    )
    last_audit_row = await cursor.fetchone()
    last_audit = dict(last_audit_row) if last_audit_row else None

    open_findings = await obs_crud.query(
        rt.db,
        source="recon",
        category="code_audit",
        resolved=False,
        limit=1000,
    )

    severity_counts: dict[str, int] = {}
    for f in open_findings:
        try:
            parsed = json.loads(f.get("content", "{}"))
            sev = parsed.get("severity", "unknown")
        except (json.JSONDecodeError, TypeError):
            sev = "unknown"
        severity_counts[sev] = severity_counts.get(sev, 0) + 1

    return jsonify({
        "last_audit": last_audit,
        "open_findings_count": len(open_findings),
        "severity_counts": severity_counts,
    })
