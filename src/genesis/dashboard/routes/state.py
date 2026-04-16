"""State endpoints: approvals, cognitive, awareness, jobs, autonomy."""

from __future__ import annotations

import json as _json
from datetime import UTC, datetime

from flask import jsonify, request

from genesis.dashboard._blueprint import _async_route, blueprint


def _parse_approval_rows(rows: list[dict]) -> list[dict]:
    now = datetime.now(UTC)
    parsed: list[dict] = []
    for row in rows:
        entry = dict(row)
        try:
            context = _json.loads(entry.get("context") or "{}")
        except (TypeError, ValueError):
            context = {}
        if not isinstance(context, dict):
            context = {}
        entry["context_data"] = context

        # Staleness indicator for pending approvals
        created = entry.get("created_at")
        if created and entry.get("status") == "pending":
            try:
                created_dt = datetime.fromisoformat(created)
                if created_dt.tzinfo is None:
                    created_dt = created_dt.replace(tzinfo=UTC)
                age = now - created_dt
                age_hours = age.total_seconds() / 3600
                entry["age_hours"] = round(age_hours, 1)
                if age_hours < 4:
                    entry["freshness"] = "fresh"
                elif age_hours < 24:
                    entry["freshness"] = "aging"
                else:
                    entry["freshness"] = "stale"
            except (ValueError, TypeError):
                entry["age_hours"] = None
                entry["freshness"] = "unknown"

        parsed.append(entry)
    return parsed


@blueprint.route("/api/genesis/approvals")
@_async_route
async def pending_approvals():
    """Return pending approval requests (module proposals, etc.)."""
    from genesis.db.crud import approval_requests
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt._db is None:
        return jsonify([])

    pending = await approval_requests.list_pending(rt._db)
    return jsonify(_parse_approval_rows(pending))


@blueprint.route("/api/genesis/approvals/<request_id>/resolve", methods=["POST"])
@_async_route
async def resolve_approval(request_id: str):
    """Resolve a pending approval request from the dashboard."""
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    gate = getattr(rt, "_autonomous_cli_approval_gate", None)
    if not rt.is_bootstrapped or gate is None:
        return jsonify({"error": "Approval gate unavailable"}), 503

    payload = request.get_json(silent=True) or {}
    decision = str(payload.get("decision") or "").strip().lower()
    if decision not in {"approved", "rejected"}:
        return jsonify({"error": "decision must be 'approved' or 'rejected'"}), 400

    ok = await gate.resolve_request(
        request_id,
        decision=decision,
        resolved_by="dashboard",
    )
    if not ok:
        return jsonify({"error": "Approval not found or no longer pending"}), 404
    return jsonify({"id": request_id, "status": decision})


@blueprint.route("/api/genesis/approvals/approve-all", methods=["POST"])
@_async_route
async def approve_all_approvals():
    """Approve all pending approval requests at once."""
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    gate = getattr(rt, "_autonomous_cli_approval_gate", None)
    if not rt.is_bootstrapped or gate is None:
        return jsonify({"error": "Approval gate unavailable"}), 503

    count = await gate.approve_all_pending(resolved_by="dashboard:batch")
    return jsonify({"approved": count})


@blueprint.route("/api/genesis/autonomous-cli-policy")
@_async_route
async def autonomous_cli_policy():
    """Return effective autonomous CLI policy plus export status."""
    from genesis.autonomy.cli_policy import load_autonomous_cli_policy
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    exporter = getattr(rt, "_autonomous_cli_policy_exporter", None)
    if exporter is not None and hasattr(exporter, "status"):
        return jsonify(exporter.status())
    return jsonify({
        "effective_policy": load_autonomous_cli_policy().as_dict(),
        "last_export_at": None,
        "last_export_path": None,
        "last_export_error": None,
    })


@blueprint.route("/api/genesis/cognitive")
@_async_route
async def cognitive_state_endpoint():
    """Return current cognitive state: active context, state flags, health flags, session patches."""
    from genesis.db.crud import cognitive_state as cs_crud
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.db is None:
        return jsonify({"active_context": None, "state_flags": None, "health_flags": "", "session_patches": []})

    active = await cs_crud.get_current(rt.db, "active_context")
    flags_row = await cs_crud.get_current(rt.db, "state_flags")
    health_flags = await cs_crud.compute_state_flags(rt.db)
    patches = cs_crud.load_session_patches()

    # Compute narrative freshness from active_context timestamp
    freshness_seconds = None
    freshness_level = "unknown"
    if active and active.get("created_at"):
        try:
            created = datetime.fromisoformat(active["created_at"])
            if created.tzinfo is None:
                created = created.replace(tzinfo=UTC)
            freshness_seconds = int((datetime.now(UTC) - created).total_seconds())
            if freshness_seconds < 3600 * 6:
                freshness_level = "fresh"
            elif freshness_seconds < 3600 * 48:
                freshness_level = "aging"
            else:
                freshness_level = "stale"
        except (ValueError, TypeError):
            pass

    return jsonify({
        "active_context": active,
        "state_flags": flags_row,
        "health_flags": health_flags,
        "session_patches": patches,
        "freshness_seconds": freshness_seconds,
        "freshness_level": freshness_level,
    })


@blueprint.route("/api/genesis/awareness/signals")
@_async_route
async def awareness_signals():
    """Return latest awareness tick + recent history with parsed signals/scores."""
    from genesis.db.crud import awareness_ticks as at_crud
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.db is None:
        return jsonify({"latest": None, "history": []})

    latest = await at_crud.last_tick(rt.db)
    history = await at_crud.query(rt.db, limit=12)

    def parse_tick(t):
        if not t:
            return t
        try:
            t["signals"] = _json.loads(t.get("signals_json") or "[]")
        except (ValueError, TypeError):
            t["signals"] = []
        try:
            t["scores"] = _json.loads(t.get("scores_json") or "[]")
        except (ValueError, TypeError):
            t["scores"] = []
        return t

    return jsonify({
        "latest": parse_tick(latest),
        "history": [parse_tick(t) for t in history],
    })


@blueprint.route("/api/genesis/jobs")
@_async_route
async def job_health_endpoint():
    """Return scheduled job health + observation retrieval stats."""
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.db is None:
        return jsonify({"jobs": [], "observations": {}})

    cursor = await rt.db.execute(
        "SELECT * FROM job_health ORDER BY job_name"
    )
    jobs = [dict(r) for r in await cursor.fetchall()]

    cursor = await rt.db.execute(
        "SELECT COUNT(*) as total, "
        "SUM(retrieved_count) as total_retrieved, "
        "SUM(CASE WHEN influenced_action = 1 THEN 1 ELSE 0 END) as total_influenced "
        "FROM observations"
    )
    obs_row = await cursor.fetchone()

    cursor = await rt.db.execute(
        "SELECT COUNT(*) FROM observations "
        "WHERE created_at > strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now', '-24 hours')"
    )
    obs_24h = (await cursor.fetchone())[0]

    return jsonify({
        "jobs": jobs,
        "observations": {
            "total": obs_row[0] or 0,
            "total_retrieved": obs_row[1] or 0,
            "total_influenced": obs_row[2] or 0,
            "created_24h": obs_24h,
        },
    })


@blueprint.route("/api/genesis/autonomy/config")
@_async_route
async def autonomy_config():
    """Return drive weights and depth thresholds."""
    from genesis.db.crud import depth_thresholds as dt_crud
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.db is None:
        return jsonify({"drives": [], "thresholds": []})

    thresholds = await dt_crud.list_all(rt.db)

    cursor = await rt.db.execute(
        "SELECT * FROM drive_weights ORDER BY drive_name"
    )
    drives = [dict(r) for r in await cursor.fetchall()]

    return jsonify({"drives": drives, "thresholds": thresholds})


@blueprint.route("/api/genesis/settings/timezone", methods=["GET", "POST"])
@_async_route
async def settings_timezone():
    """Get or set the user's display timezone.

    GET: returns {"timezone": <IANA tz name>} — whatever the user configured
         (defaults to "UTC" if unset).
    POST: {"timezone": <IANA tz name>} → validates, writes to genesis.yaml,
          invalidates caches. Takes effect immediately for display;
          scheduler CronTrigger timezone updates on next restart.
    """
    from genesis.env import user_timezone

    if request.method == "GET":
        return jsonify({"timezone": user_timezone()})

    payload = request.get_json(silent=True) or {}
    new_tz = payload.get("timezone", "").strip()
    if not new_tz:
        return jsonify({"error": "timezone is required"}), 400

    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    try:
        ZoneInfo(new_tz)
    except (ZoneInfoNotFoundError, KeyError):
        return jsonify({"error": f"Invalid timezone: {new_tz}"}), 400

    # Write to genesis.yaml
    import tempfile
    from pathlib import Path
    cfg_path = Path.home() / ".genesis" / "config" / "genesis.yaml"
    try:
        import yaml
        existing = {}
        if cfg_path.is_file():
            with cfg_path.open() as fh:
                existing = yaml.safe_load(fh) or {}
        existing["timezone"] = new_tz
        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=str(cfg_path.parent), suffix=".yaml.tmp",
        )
        try:
            import os
            with os.fdopen(tmp_fd, "w") as f:
                yaml.dump(existing, f, default_flow_style=False)
            os.replace(tmp_path, str(cfg_path))
        except Exception:
            os.unlink(tmp_path)
            raise
    except Exception as exc:
        return jsonify({"error": f"Failed to write config: {exc}"}), 500

    # Invalidate caches
    from genesis.env import _invalidate_local_config
    _invalidate_local_config()

    from genesis.util.tz import reload as tz_reload
    effective = tz_reload()

    return jsonify({
        "timezone": effective,
        "note": "Scheduler timezone updates on next restart",
    })
