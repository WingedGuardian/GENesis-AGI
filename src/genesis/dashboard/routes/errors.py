"""Unified errors and deferred work management routes."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from flask import jsonify, request

from genesis.dashboard._blueprint import _async_route, blueprint

logger = logging.getLogger(__name__)


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
        # Backend not ready / DB unavailable: no error data could be loaded, so this
        # is NOT a clean "0 errors" — flag it partial so the Errors tab shows the
        # degraded banner instead of "data is clean".
        return jsonify({"groups": [], "active_alerts": [], "totals": {"events": 0, "dead_letters": 0, "deferred_failures": 0}, "partial": True, "sources_failed": ["backend unavailable"]})

    since = request.args.get("since")
    if not since:
        since = (datetime.now(UTC) - timedelta(hours=24)).isoformat()
    grouped = request.args.get("grouped", "true").lower() in ("true", "1", "yes")
    subsystem_filter = request.args.get("subsystem")
    limit = min(request.args.get("limit", 50, type=int), 200)
    # Source rows are GROUPED before display, so the display limit must not cap
    # the scan: capping it makes every derived total a truncated read that looks
    # like a real number. That is the defect this endpoint shipped — the
    # attention strip rendered "6 active error groups" for any backlog >= 6,
    # because `limit=6` capped the three source reads the groups were built
    # from. Scan generously, group, publish true totals, then slice for display.
    _SCAN_CAP = 1000
    scan_limit = max(limit, _SCAN_CAP)
    scan_truncated = False

    now = datetime.now(UTC)
    thirty_min_ago = (now - timedelta(minutes=30)).isoformat()

    event_count = 0
    dl_count = 0
    dw_count = 0
    # Each data source is queried independently; a failed source must be SURFACED
    # (partial/sources_failed) so a DB/FTS outage can't read as a clean "0 errors".
    sources_failed: list[str] = []

    groups: list[dict] = []
    try:
        if grouped:
            raw_groups = await events_crud.query_grouped_errors(
                rt.db, since=since, subsystem=subsystem_filter, limit=scan_limit,
            )
            scan_truncated = scan_truncated or len(raw_groups) >= scan_limit
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
        logger.warning("unified-errors: events source failed", exc_info=True)
        sources_failed.append("events")

    try:
        dl_items = await dl_crud.query_recent(
            rt.db, since=since, limit=scan_limit,
        )
        scan_truncated = scan_truncated or len(dl_items) >= scan_limit
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
        logger.warning("unified-errors: dead_letters source failed", exc_info=True)
        sources_failed.append("dead_letters")

    try:
        dw_items = await dw_crud.query_failed(
            rt.db, since=since, limit=scan_limit,
        )
        scan_truncated = scan_truncated or len(dw_items) >= scan_limit
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
        logger.warning("unified-errors: deferred_work source failed", exc_info=True)
        sources_failed.append("deferred_work")

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
        logger.warning("unified-errors: resolutions overlay failed", exc_info=True)
        sources_failed.append("resolutions")

    groups.sort(key=lambda g: g["last_seen"], reverse=True)

    active_alerts: list[dict] = []
    try:
        from genesis.mcp.health_mcp import _impl_health_alerts
        active_alerts = await _impl_health_alerts(active_only=True)
    except Exception:
        logger.warning("unified-errors: active_alerts source failed", exc_info=True)
        sources_failed.append("alerts")

    # `groups` is the FULL grouped set (built from a scan_limit-wide read);
    # `groups[:limit]` is the display page. The totals below therefore describe
    # the real population, not the page — rendering len(page) as a total is the
    # defect this endpoint shipped. `groups_totals_truncated` is the loud
    # marker for the one case the totals are still a lower bound: a source that
    # filled the entire scan window.
    return jsonify({
        "groups": groups[:limit],
        "groups_total": len(groups),
        "groups_active_total": sum(1 for g in groups if g.get("still_active")),
        "groups_totals_truncated": scan_truncated,
        "active_alerts": active_alerts,
        "totals": {
            "events": event_count,
            "dead_letters": dl_count,
            "deferred_failures": dw_count,
        },
        "partial": bool(sources_failed),
        "sources_failed": sources_failed,
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
    # Unconditional by design. The queues snapshot is cached up to 30s, and a
    # `cleared == 0` result is the STRONGEST signal the caller's view is stale
    # (it clicked Clear on a row the cache still shows but the DB no longer
    # has) — precisely when the cache most needs busting. Guarding on `cleared`
    # would skip the phantom-row case this exists to fix, and `rowcount` is -1
    # (truthy) on some drivers anyway.
    from genesis.dashboard.routes.health import invalidate_snapshot_cache

    invalidate_snapshot_cache()
    return jsonify({"cleared": cleared})
