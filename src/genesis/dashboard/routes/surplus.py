"""Surplus scheduler dashboard routes — consolidated from outreach.py and recon.py.

Endpoints:
- GET  /api/genesis/surplus/detail   — task history, stats, catalog
- GET  /api/genesis/surplus/activity — code audit stats
- GET  /api/genesis/surplus/config   — full panel data (config + catalog + drives + eval)
- PUT  /api/genesis/surplus/config   — update surplus config via local overlay
- PUT  /api/genesis/surplus/drives   — update drive weights
"""

from __future__ import annotations

import json
import logging
from datetime import datetime

import aiosqlite
from flask import jsonify, request

from genesis.dashboard._blueprint import _async_route, blueprint

logger = logging.getLogger(__name__)


@blueprint.route("/api/genesis/surplus/detail")
@_async_route
async def surplus_detail():
    """Return detailed surplus task history and queue contents."""
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.db is None:
        return jsonify({"error": "not bootstrapped"}), 503

    rt.db.row_factory = aiosqlite.Row

    cursor = await rt.db.execute(
        """SELECT id, task_type, status, compute_tier, priority, drive_alignment,
                  created_at, started_at, completed_at, failure_reason, attempt_count
           FROM surplus_tasks
           ORDER BY created_at DESC LIMIT 100"""
    )
    all_tasks = []
    stats = {"completed": 0, "failed": 0, "pending": 0, "stub_completions": 0}
    failure_reasons: dict[str, int] = {}
    for r in await cursor.fetchall():
        task = dict(r)
        stats[task["status"]] = stats.get(task["status"], 0) + 1
        if task.get("started_at") and task.get("completed_at"):
            try:
                start = datetime.fromisoformat(task["started_at"])
                end = datetime.fromisoformat(task["completed_at"])
                dur_s = (end - start).total_seconds()
                task["duration_s"] = round(dur_s, 2)
                if dur_s < 1.0 and task["status"] == "completed":
                    task["stub"] = True
                    stats["stub_completions"] += 1
            except (ValueError, TypeError):
                task["duration_s"] = None
        if task.get("failure_reason"):
            fr = task["failure_reason"]
            failure_reasons[fr] = failure_reasons.get(fr, 0) + 1
        all_tasks.append(task)

    try:
        from genesis.surplus.types import ComputeTier, TaskType
        catalog = [{"type": t.value, "tier": ComputeTier.FREE_API.value} for t in TaskType]
    except Exception:
        catalog = []

    return jsonify({
        "tasks": all_tasks,
        "stats": stats,
        "failure_reasons": failure_reasons,
        "catalog": catalog,
    })


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


@blueprint.route("/api/genesis/surplus/config")
@_async_route
async def surplus_config():
    """Return full surplus panel data: config, catalog, drives, eval staleness."""
    from genesis.db.crud import drive_weights as dw_crud
    from genesis.mcp.health.settings import _load_yaml_merged
    from genesis.observability.snapshots.eval_staleness import eval_staleness
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    db = rt.db if rt.is_bootstrapped else None
    if db is not None:
        db.row_factory = aiosqlite.Row

    # Config
    config = _load_yaml_merged("surplus.yaml")

    # Task catalog with last-run times
    catalog = []
    try:
        from genesis.surplus.types import TaskType
        catalog = [{"type": t.value} for t in TaskType]
        if db is not None:
            for item in catalog:
                cursor = await db.execute(
                    """SELECT status, created_at, completed_at FROM surplus_tasks
                       WHERE task_type = ? ORDER BY created_at DESC LIMIT 1""",
                    (item["type"],),
                )
                row = await cursor.fetchone()
                if row:
                    r = dict(row)
                    item["last_status"] = r.get("status")
                    item["last_run"] = r.get("completed_at") or r.get("created_at")
    except Exception:
        logger.warning("Failed to build surplus catalog", exc_info=True)

    # Drive weights
    drives = []
    if db is not None:
        try:
            drives = await dw_crud.get_all(db)
        except Exception:
            logger.warning("Failed to read drive weights", exc_info=True)

    # Eval staleness
    eval_data = await eval_staleness(db)

    # Queue stats
    stats = {}
    if db is not None:
        try:
            cursor = await db.execute(
                "SELECT status, COUNT(*) as cnt FROM surplus_tasks GROUP BY status"
            )
            for row in await cursor.fetchall():
                r = dict(row)
                stats[r["status"]] = r["cnt"]
        except Exception:
            logger.warning("Failed to read surplus stats", exc_info=True)

    return jsonify({
        "config": config,
        "catalog": catalog,
        "drives": drives,
        "eval_staleness": eval_data,
        "stats": stats,
    })


@blueprint.route("/api/genesis/surplus/config", methods=["PUT"])
@_async_route
async def update_surplus_config():
    """Update surplus config via local overlay."""
    from genesis.mcp.health.settings import (
        _atomic_yaml_write,
        _load_yaml_local,
        _load_yaml_merged,
        _local_filename,
        _validate_surplus,
    )

    data = request.get_json(silent=True) or {}
    if not data:
        return jsonify({"error": "No data provided"}), 400

    errors = _validate_surplus(data)
    if errors:
        return jsonify({"error": "Validation failed", "details": errors}), 422

    # Merge into local overlay — NEVER write to the base config
    local = _load_yaml_local("surplus.yaml")
    _deep_merge(local, data)
    _atomic_yaml_write(_local_filename("surplus.yaml"), local)

    merged = _load_yaml_merged("surplus.yaml")
    return jsonify({"ok": True, "config": merged})


@blueprint.route("/api/genesis/surplus/drives", methods=["PUT"])
@_async_route
async def update_surplus_drives():
    """Update drive weights."""
    from genesis.db.crud import drive_weights as dw_crud
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.db is None:
        return jsonify({"error": "not bootstrapped"}), 503

    data = request.get_json(silent=True) or {}
    updates = data.get("drives", {})
    if not updates:
        return jsonify({"error": "No drives provided"}), 400

    valid_drives = {"competence", "cooperation", "curiosity", "preservation"}
    results = {}
    for drive_name, weight in updates.items():
        if drive_name not in valid_drives:
            results[drive_name] = {"ok": False, "error": f"Unknown drive: {drive_name}"}
            continue
        try:
            weight = float(weight)
            await dw_crud.update_weight(rt.db, drive_name, weight)
            results[drive_name] = {"ok": True, "weight": weight}
        except Exception as e:
            results[drive_name] = {"ok": False, "error": str(e)}

    return jsonify({"results": results})


def _deep_merge(base: dict, overlay: dict) -> dict:
    """Recursive merge — delegates to settings module implementation."""
    from genesis.mcp.health.settings import _deep_merge as _dm

    return _dm(base, overlay)
