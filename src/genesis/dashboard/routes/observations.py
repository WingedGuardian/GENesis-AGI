"""Dashboard routes for the Observations panel."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from flask import jsonify, request

from genesis.dashboard._blueprint import _async_route, blueprint

logger = logging.getLogger(__name__)


def _observation_status(row: dict) -> str:
    """Derive an observation's lifecycle stage from its existing fields.

    Stages, highest-precedence first:
      - ``resolved``  — condition cleared (resolved=1)
      - ``acted``     — drove a downstream artifact (influenced_action=1)
      - ``read``      — a consumer/user has seen it (retrieved_count>0 or surfaced_at set)
      - ``new``       — unread + active (still "blaring")

    Pure derivation — no schema change. Backend-authoritative so the dashboard
    badge and any future consumer agree on the stage.
    """
    if row.get("resolved"):
        return "resolved"
    if row.get("influenced_action"):
        return "acted"
    if (row.get("retrieved_count") or 0) > 0 or row.get("surfaced_at"):
        return "read"
    return "new"


@blueprint.route("/api/genesis/observations")
@_async_route
async def observations_list():
    """Return observations with optional filters."""
    from genesis.db.crud import observations as obs_crud
    from genesis.db.crud.observations import INTERNAL_OBS_TYPES
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.db is None:
        return jsonify({"observations": [], "has_more": False})

    priority = request.args.get("priority") or None
    obs_type = request.args.get("type") or None
    source = request.args.get("source") or None
    resolved_str = request.args.get("resolved")
    show_internal = request.args.get("internal", "false").lower() == "true"
    limit = min(request.args.get("limit", 50, type=int), 200)

    resolved = None
    if resolved_str is not None:
        resolved = resolved_str.lower() == "true"

    kwargs: dict = {"limit": limit + 1}
    if priority:
        kwargs["priority"] = priority
    if obs_type:
        kwargs["type"] = obs_type
    if source == "session:*":
        # Sentinel from the filters endpoint: all session-sourced observations
        # collapse into one dropdown option (raw values are per-session UUIDs).
        kwargs["source_prefix"] = "session:"
    elif source:
        kwargs["source"] = source
    if resolved is not None:
        kwargs["resolved"] = resolved
    if not show_internal:
        kwargs["exclude_types"] = INTERNAL_OBS_TYPES

    rows = await obs_crud.query(rt.db, **kwargs)

    has_more = len(rows) > limit
    rows = rows[:limit]

    # Derive the lifecycle stage (new/read/acted/resolved) for the panel badge.
    for r in rows:
        r["status"] = _observation_status(r)

    return jsonify({"observations": rows, "has_more": has_more})


@blueprint.route("/api/genesis/observations/summary")
@_async_route
async def observations_summary():
    """Return observation counts for dashboard badges."""
    from genesis.db.crud import observations as obs_crud
    from genesis.db.crud.observations import INTERNAL_OBS_TYPES
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.db is None:
        return jsonify({"counts": {}, "total_unsurfaced": 0, "total_unresolved": 0})

    counts = await obs_crud.unsurfaced_counts_by_priority(rt.db)
    # Filter out internal types from the counts shown to user. COUNT query —
    # this endpoint is polled every 15s by every open dashboard for the badge.
    total_unsurfaced = await obs_crud.count_unsurfaced(
        rt.db,
        priority_filter=("critical", "high", "medium", "low"),
        exclude_types=tuple(INTERNAL_OBS_TYPES),
    )
    total_unresolved = await obs_crud.count_unresolved(
        rt.db, exclude_types=INTERNAL_OBS_TYPES
    )

    return jsonify({
        "counts": counts,
        "total_unsurfaced": total_unsurfaced,
        "total_unresolved": total_unresolved,
    })


@blueprint.route("/api/genesis/observations/filters")
@_async_route
async def observations_filters():
    """Return distinct types and sources for filter dropdowns."""
    from genesis.db.crud import observations as obs_crud
    from genesis.db.crud.observations import INTERNAL_OBS_TYPES
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.db is None:
        return jsonify({"types": [], "sources": []})

    # Only return types/sources that have unresolved observations
    all_types = await obs_crud.distinct_unresolved_types(rt.db)
    # Exclude internal types from the dropdown
    types = [t for t in all_types if t not in INTERNAL_OBS_TYPES]

    # Collapse per-session UUID sources ("session:<uuid>") into one sentinel
    # option; the list endpoint expands "session:*" to a prefix match. Internal
    # types are excluded here for the same reason they are excluded from the
    # list by default — a source with only internal rows would filter to zero.
    all_sources = await obs_crud.distinct_unresolved_sources(
        rt.db, exclude_types=tuple(INTERNAL_OBS_TYPES)
    )
    sources = [s for s in all_sources if not s.startswith("session:")]
    if len(sources) != len(all_sources):
        sources.append("session:*")

    return jsonify({"types": types, "sources": sources})


@blueprint.route("/api/genesis/observations/<obs_id>/resolve", methods=["POST"])
@_async_route
async def observations_resolve(obs_id: str):
    """Resolve a single observation."""
    from genesis.db.crud import observations as obs_crud
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.db is None:
        return jsonify({"ok": False, "error": "Not bootstrapped"}), 503

    data = request.get_json(silent=True) or {}
    notes = data.get("notes", "")
    resolved_at = datetime.now(UTC).isoformat()

    success = await obs_crud.resolve(
        rt.db, obs_id, resolved_at=resolved_at, resolution_notes=notes
    )

    if not success:
        return jsonify({"ok": False, "error": "Not found"}), 404

    return jsonify({"ok": True, "resolved_at": resolved_at})


@blueprint.route("/api/genesis/observations/<obs_id>/mark-read", methods=["POST"])
@_async_route
async def observations_mark_read(obs_id: str):
    """Mark a single observation as surfaced (read)."""
    from genesis.db.crud import observations as obs_crud
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.db is None:
        return jsonify({"ok": False, "error": "Not bootstrapped"}), 503

    surfaced_at = datetime.now(UTC).isoformat()
    count = await obs_crud.mark_surfaced(rt.db, [obs_id], surfaced_at)

    return jsonify({"ok": count > 0, "surfaced_at": surfaced_at})


@blueprint.route("/api/genesis/observations/batch", methods=["POST"])
@_async_route
async def observations_batch():
    """Batch resolve or mark-read observations."""
    from genesis.db.crud import observations as obs_crud
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.db is None:
        return jsonify({"ok": False, "error": "Not bootstrapped"}), 503

    data = request.get_json(silent=True) or {}
    action = data.get("action")
    ids = data.get("ids", [])
    notes = data.get("notes", "")

    if not ids or action not in ("resolve", "mark_read"):
        return jsonify({"ok": False, "error": "Invalid action or empty ids"}), 400
    if len(ids) > 200:
        return jsonify({"ok": False, "error": "Too many ids (max 200)"}), 400

    now = datetime.now(UTC).isoformat()

    if action == "mark_read":
        count = await obs_crud.mark_surfaced(rt.db, ids, now)
    else:
        count = await obs_crud.resolve_batch(
            rt.db, ids, resolved_at=now, resolution_notes=notes
        )

    return jsonify({"ok": True, "count": count})
