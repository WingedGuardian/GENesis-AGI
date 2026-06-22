"""Follow-up accountability routes for the dashboard.

Exposes follow-up list with status counts, visible alongside tasks.
"""

from __future__ import annotations

import logging

from flask import jsonify, request

from genesis.dashboard._blueprint import _async_route, blueprint

logger = logging.getLogger(__name__)


@blueprint.route("/api/genesis/follow-ups")
@_async_route
async def follow_up_list():
    """Return follow-ups with optional status filter.

    Query params:
        status – filter by status (default: all)
        limit – max results (default 30)
    """
    from genesis.db.crud import follow_ups
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.db is None:
        return jsonify({"follow_ups": [], "counts": {}})

    status_filter = request.args.get("status", "").strip() or None
    source_filter = request.args.get("source", "").strip() or None
    source_mode = request.args.get("source_mode", "all").strip()
    limit = min(request.args.get("limit", 30, type=int), 200)

    # Backward compat: source=user maps to source_mode=mine
    if source_filter == "user" and source_mode == "all":
        source_mode = "mine"

    try:
        if status_filter:
            items = await follow_ups.get_by_status(rt.db, status_filter)
            # Apply source_mode filter post-query for status-filtered results
            if source_mode == "mine":
                items = [i for i in items if i.get("source") == "foreground_session"]
            elif source_mode == "system":
                items = [i for i in items if i.get("source") != "foreground_session"]
            items = items[:limit]
        else:
            items = await follow_ups.get_recent(
                rt.db, limit=limit, source_mode=source_mode,
            )

        counts = await follow_ups.get_summary_counts(rt.db)
    except Exception:
        logger.error("Failed to list follow-ups", exc_info=True)
        return jsonify({"follow_ups": [], "counts": {}})

    return jsonify({
        "follow_ups": items,
        "counts": counts,
        "total": sum(counts.values()),
    })


@blueprint.route("/api/genesis/follow-ups/summary")
@_async_route
async def follow_up_summary():
    """Return just the counts by status for dashboard badges."""
    from genesis.db.crud import follow_ups
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.db is None:
        return jsonify({"counts": {}, "total": 0})

    try:
        counts = await follow_ups.get_summary_counts(rt.db)
        return jsonify({"counts": counts, "total": sum(counts.values())})
    except Exception:
        logger.error("Failed to get follow-up summary", exc_info=True)
        return jsonify({"counts": {}, "total": 0})


# ── Cockpit: the dedicated "Follow-ups" management tab ──────────────────
# Read routes return an empty 200 when not bootstrapped (panel renders empty);
# mutation routes return (…, 503) so the UI can surface a real failure.

_COCKPIT_STATUSES = (
    "pending", "scheduled", "in_progress", "completed", "failed", "blocked",
)
_BATCH_ACTIONS = ("done", "delete", "tabled", "follow_up")


@blueprint.route("/api/genesis/follow-ups/cockpit")
@_async_route
async def follow_up_cockpit():
    """Paginated/sorted/filtered follow-up list for the cockpit tab.

    Query params: kind (follow_up|tabled|all), domain (internal|user_world|
    __null__), status, source, search, sort, page, page_size.
    """
    from genesis.db.crud import follow_ups
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    empty = {"items": [], "total": 0, "page": 1, "page_size": 50}
    if not rt.is_bootstrapped or rt.db is None:
        return jsonify(empty)

    kind = request.args.get("kind", "").strip() or None
    if kind == "all":
        kind = None
    domain = request.args.get("domain", "").strip() or None
    status = request.args.get("status", "").strip() or None
    source = request.args.get("source", "").strip() or None
    search = request.args.get("search", "").strip() or None
    sort = request.args.get("sort", "priority").strip() or "priority"
    page = max(1, request.args.get("page", 1, type=int))
    page_size = min(max(request.args.get("page_size", 50, type=int), 1), 200)

    try:
        items = await follow_ups.query_page(
            rt.db, kind=kind, domain=domain, status=status, source=source,
            search=search, sort=sort, offset=(page - 1) * page_size,
            limit=page_size,
        )
        total = await follow_ups.count_filtered(
            rt.db, kind=kind, domain=domain, status=status, source=source,
            search=search,
        )
    except Exception:
        logger.error("Failed to query follow-up cockpit", exc_info=True)
        return jsonify(empty)

    return jsonify({
        "items": items, "total": total, "page": page, "page_size": page_size,
    })


@blueprint.route("/api/genesis/follow-ups/filters")
@_async_route
async def follow_up_filters():
    """Distinct sources (dynamic) + the fixed status set, for cockpit dropdowns."""
    from genesis.db.crud import follow_ups
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.db is None:
        return jsonify({"sources": [], "statuses": list(_COCKPIT_STATUSES)})

    try:
        sources = await follow_ups.get_distinct_sources(rt.db)
    except Exception:
        logger.error("Failed to load follow-up filters", exc_info=True)
        sources = []
    return jsonify({"sources": sources, "statuses": list(_COCKPIT_STATUSES)})


@blueprint.route("/api/genesis/follow-ups/<fid>/done", methods=["POST"])
@_async_route
async def follow_up_done(fid: str):
    """Mark a follow-up Done (soft — status=completed, row kept)."""
    from genesis.db.crud import follow_ups
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.db is None:
        return jsonify({"ok": False, "error": "Not bootstrapped"}), 503

    notes = (request.get_json(silent=True) or {}).get("notes") or None
    ok = await follow_ups.update_status(
        rt.db, fid, "completed", resolution_notes=notes,
    )
    if not ok:
        return jsonify({"ok": False, "error": "Not found"}), 404
    return jsonify({"ok": True})


@blueprint.route("/api/genesis/follow-ups/<fid>/delete", methods=["POST"])
@_async_route
async def follow_up_delete(fid: str):
    """Permanently delete a follow-up (UI gates this behind a confirm)."""
    from genesis.db.crud import follow_ups
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.db is None:
        return jsonify({"ok": False, "error": "Not bootstrapped"}), 503

    ok = await follow_ups.delete(rt.db, fid)
    if not ok:
        return jsonify({"ok": False, "error": "Not found"}), 404
    return jsonify({"ok": True})


@blueprint.route("/api/genesis/follow-ups/<fid>/pin", methods=["POST"])
@_async_route
async def follow_up_pin(fid: str):
    """Pin/unpin a follow-up."""
    from genesis.db.crud import follow_ups
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.db is None:
        return jsonify({"ok": False, "error": "Not bootstrapped"}), 503

    pinned = bool((request.get_json(silent=True) or {}).get("pinned"))
    ok = await follow_ups.set_pinned(rt.db, fid, pinned)
    if not ok:
        return jsonify({"ok": False, "error": "Not found"}), 404
    return jsonify({"ok": True, "pinned": pinned})


@blueprint.route("/api/genesis/follow-ups/<fid>/priority", methods=["POST"])
@_async_route
async def follow_up_priority(fid: str):
    """Change a follow-up's priority."""
    from genesis.db.crud import follow_ups
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.db is None:
        return jsonify({"ok": False, "error": "Not bootstrapped"}), 503

    priority = (request.get_json(silent=True) or {}).get("priority", "")
    try:
        ok = await follow_ups.set_priority(rt.db, fid, priority)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    if not ok:
        return jsonify({"ok": False, "error": "Not found"}), 404
    return jsonify({"ok": True, "priority": priority})


@blueprint.route("/api/genesis/follow-ups/<fid>/kind", methods=["POST"])
@_async_route
async def follow_up_kind(fid: str):
    """Move a follow-up between the follow_up and tabled lanes."""
    from genesis.db.crud import follow_ups
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.db is None:
        return jsonify({"ok": False, "error": "Not bootstrapped"}), 503

    kind = (request.get_json(silent=True) or {}).get("kind", "")
    try:
        ok = await follow_ups.set_kind(rt.db, fid, kind)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    if not ok:
        return jsonify({"ok": False, "error": "Not found"}), 404
    return jsonify({"ok": True, "kind": kind})


@blueprint.route("/api/genesis/follow-ups/<fid>/domain", methods=["POST"])
@_async_route
async def follow_up_domain(fid: str):
    """Override a follow-up's domain (or clear it with an empty value)."""
    from genesis.db.crud import follow_ups
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.db is None:
        return jsonify({"ok": False, "error": "Not bootstrapped"}), 503

    domain = (request.get_json(silent=True) or {}).get("domain") or None
    try:
        ok = await follow_ups.set_domain(rt.db, fid, domain)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    if not ok:
        return jsonify({"ok": False, "error": "Not found"}), 404
    return jsonify({"ok": True, "domain": domain})


@blueprint.route("/api/genesis/follow-ups/batch", methods=["POST"])
@_async_route
async def follow_up_batch():
    """Batch action over a selection (max 200): done/delete/tabled/follow_up."""
    from genesis.db.crud import follow_ups
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.db is None:
        return jsonify({"ok": False, "error": "Not bootstrapped"}), 503

    data = request.get_json(silent=True) or {}
    action = data.get("action")
    ids = data.get("ids", [])
    notes = data.get("notes") or None

    if not ids or action not in _BATCH_ACTIONS:
        return jsonify({"ok": False, "error": "Invalid action or empty ids"}), 400
    if len(ids) > 200:
        return jsonify({"ok": False, "error": "Too many ids (max 200)"}), 400

    try:
        if action == "done":
            count = await follow_ups.update_status_batch(
                rt.db, ids, "completed", resolution_notes=notes,
            )
        elif action == "delete":
            count = await follow_ups.delete_batch(rt.db, ids)
        else:  # "tabled" | "follow_up"
            count = await follow_ups.set_kind_batch(rt.db, ids, action)
    except Exception:
        logger.error("Batch follow-up action %r failed", action, exc_info=True)
        return jsonify({"ok": False, "error": "Batch action failed"}), 503

    return jsonify({"ok": True, "count": count})
