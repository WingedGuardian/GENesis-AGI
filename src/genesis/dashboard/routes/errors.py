"""Unified errors and deferred work management routes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from flask import jsonify, request

from genesis.dashboard._blueprint import _async_route, blueprint


@blueprint.route("/api/genesis/unified-errors")
@_async_route
async def unified_errors():
    """Unified error view: WARNING+ events + dead letters + failed deferred work."""
    from genesis.db.crud import dead_letter as dl_crud
    from genesis.db.crud import deferred_work as dw_crud
    from genesis.db.crud import events as events_crud
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.db is None:
        return jsonify({"groups": [], "active_alerts": [], "totals": {"events": 0, "dead_letters": 0, "deferred_failures": 0}})

    since = request.args.get("since")
    if not since:
        since = (datetime.now(UTC) - timedelta(hours=24)).isoformat()
    grouped = request.args.get("grouped", "true").lower() in ("true", "1", "yes")
    subsystem_filter = request.args.get("subsystem")
    limit = min(request.args.get("limit", 50, type=int), 200)

    now = datetime.now(UTC)
    thirty_min_ago = (now - timedelta(minutes=30)).isoformat()

    event_count = 0
    dl_count = 0
    dw_count = 0

    groups: list[dict] = []
    try:
        if grouped:
            raw_groups = await events_crud.query_grouped_errors(
                rt.db, since=since, subsystem=subsystem_filter, limit=limit,
            )
            event_count = sum(g["count"] for g in raw_groups)
            for g in raw_groups:
                groups.append({
                    "key": f"events:{g['subsystem']}:{g['event_type']}:{g['msg_prefix']}",
                    "source": "events",
                    "subsystem": g["subsystem"],
                    "event_type": g["event_type"],
                    "worst_severity": g["worst_severity"],
                    "message_prefix": g["msg_prefix"],
                    "count": g["count"],
                    "first_seen": g["first_seen"],
                    "last_seen": g["last_seen"],
                    "still_active": g["last_seen"] >= thirty_min_ago,
                })
    except Exception:
        pass

    try:
        dl_items = await dl_crud.query_recent(
            rt.db, since=since, limit=limit,
        )
        dl_count = len(dl_items)
        if grouped and dl_items:
            dl_groups: dict[str, dict] = {}
            for item in dl_items:
                key = f"dead_letter:{item['target_provider']}:{item['operation_type']}:{item['failure_reason'][:80]}"
                if key not in dl_groups:
                    dl_groups[key] = {
                        "key": key,
                        "source": "dead_letter",
                        "subsystem": "routing",
                        "event_type": item["operation_type"],
                        "worst_severity": "warning",
                        "message_prefix": item["failure_reason"][:80],
                        "count": 0,
                        "first_seen": item["created_at"],
                        "last_seen": item["created_at"],
                        "still_active": False,
                    }
                g = dl_groups[key]
                g["count"] += 1
                if item["created_at"] < g["first_seen"]:
                    g["first_seen"] = item["created_at"]
                if item["created_at"] > g["last_seen"]:
                    g["last_seen"] = item["created_at"]
                g["still_active"] = g["last_seen"] >= thirty_min_ago
            groups.extend(dl_groups.values())
    except Exception:
        pass

    try:
        dw_items = await dw_crud.query_failed(
            rt.db, since=since, limit=limit,
        )
        dw_count = len(dw_items)
        if grouped and dw_items:
            dw_groups: dict[str, dict] = {}
            for item in dw_items:
                reason = (item.get("error_message") or item.get("deferred_reason") or "unknown")[:80]
                key = f"deferred_work:{item['work_type']}:{item['status']}:{reason}"
                if key not in dw_groups:
                    dw_groups[key] = {
                        "key": key,
                        "source": "deferred_work",
                        "subsystem": "resilience",
                        "event_type": item["work_type"],
                        "worst_severity": "warning",
                        "message_prefix": reason,
                        "count": 0,
                        "first_seen": item["created_at"],
                        "last_seen": item["created_at"],
                        "still_active": False,
                    }
                g = dw_groups[key]
                g["count"] += 1
                if item["created_at"] < g["first_seen"]:
                    g["first_seen"] = item["created_at"]
                if item["created_at"] > g["last_seen"]:
                    g["last_seen"] = item["created_at"]
                g["still_active"] = g["last_seen"] >= thirty_min_ago
            groups.extend(dw_groups.values())
    except Exception:
        pass

    try:
        cursor = await rt.db.execute("SELECT error_group_key, resolved_by, resolved_at, notes FROM resolved_errors")
        resolutions = {row[0]: {"resolved_by": row[1], "resolved_at": row[2], "notes": row[3]} for row in await cursor.fetchall()}
        for g in groups:
            if g["key"] in resolutions:
                r = resolutions[g["key"]]
                g["still_active"] = False
                g["manually_resolved"] = True
                g["resolved_by"] = r["resolved_by"]
                g["resolved_at"] = r["resolved_at"]
    except Exception:
        pass

    groups.sort(key=lambda g: g["last_seen"], reverse=True)

    active_alerts: list[dict] = []
    try:
        from genesis.mcp.health_mcp import _impl_health_alerts
        active_alerts = await _impl_health_alerts(active_only=True)
    except Exception:
        pass

    return jsonify({
        "groups": groups[:limit],
        "active_alerts": active_alerts,
        "totals": {
            "events": event_count,
            "dead_letters": dl_count,
            "deferred_failures": dw_count,
        },
    })


@blueprint.route("/api/genesis/deferred/<item_id>/clear", methods=["DELETE"])
@_async_route
async def clear_deferred_item(item_id):
    """Clear a discarded/expired deferred work item after user review."""
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.db is None:
        return jsonify({"error": "not bootstrapped"}), 503

    if item_id == "all":
        cur = await rt.db.execute(
            "DELETE FROM deferred_work_queue WHERE status IN ('discarded', 'expired')"
        )
    else:
        cur = await rt.db.execute(
            "DELETE FROM deferred_work_queue WHERE id = ? AND status IN ('discarded', 'expired')",
            (item_id,),
        )
    await rt.db.commit()
    cleared = cur.rowcount
    return jsonify({"cleared": cleared})
