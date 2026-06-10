"""J-9 eval metrics routes — compounding intelligence dashboard."""

from __future__ import annotations

import logging

from flask import jsonify

from genesis.dashboard._blueprint import _async_route, blueprint

logger = logging.getLogger(__name__)

_DIMENSIONS = ("memory", "system", "ego", "cognitive", "procedure")

# Which metric to extract as the "headline" value per dimension
_HEADLINE_METRIC = {
    "memory": "precision_at_5",
    "system": "composite_score",
    "ego": "approval_rate",
    "cognitive": "delta",
    "procedure": "success_rate",
}


@blueprint.route("/api/genesis/metrics/compounding")
@_async_route
async def metrics_compounding():
    """Return 12-week eval snapshot series for all dimensions."""
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt._db is None:
        return jsonify({"error": "not bootstrapped", "dimensions": {}}), 503

    from genesis.db.crud import j9_eval

    dimensions = {}
    for dim in _DIMENSIONS:
        snapshots = await j9_eval.get_snapshots(
            rt._db, dimension=dim, period_type="weekly", limit=12,
        )
        # Reverse to chronological order (oldest first for sparkline)
        snapshots.reverse()

        headline_key = _HEADLINE_METRIC.get(dim, "")
        series = []
        for snap in snapshots:
            metrics = snap.get("metrics", {})
            series.append({
                "period_end": snap.get("period_end"),
                "value": metrics.get(headline_key),
                "sample_count": snap.get("sample_count", 0),
                "metrics": metrics,
            })

        latest = snapshots[-1] if snapshots else None
        dimensions[dim] = {
            "headline_metric": headline_key,
            "current_value": (
                latest.get("metrics", {}).get(headline_key) if latest else None
            ),
            "series": series,
            "weeks_of_data": len(series),
        }

    # Compute trend direction for each dimension
    for _dim, data in dimensions.items():
        values = [
            p["value"] for p in data["series"]
            if p["value"] is not None
        ]
        if len(values) >= 2:
            # Simple: compare first half mean to second half mean
            mid = len(values) // 2
            first_half = sum(values[:mid]) / mid if mid else 0
            second_half = sum(values[mid:]) / (len(values) - mid) if (len(values) - mid) else 0
            data["trend"] = "up" if second_half > first_half else (
                "down" if second_half < first_half else "flat"
            )
        else:
            data["trend"] = "insufficient_data"

    return jsonify({"dimensions": dimensions})


@blueprint.route("/api/genesis/eval/health")
@_async_route
async def eval_health():
    """Check eval pipeline health: has data been produced recently?"""
    from datetime import UTC, datetime

    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt._db is None:
        return jsonify({"status": "no_db"}), 503

    cursor = await rt._db.execute(
        "SELECT created_at FROM eval_snapshots ORDER BY created_at DESC LIMIT 1",
    )
    row = await cursor.fetchone()

    if not row:
        return jsonify({
            "status": "no_data",
            "last_snapshot_at": None,
            "age_days": None,
            "message": "No eval snapshots exist yet",
        })

    last_at = row[0] if isinstance(row, tuple) else row["created_at"]
    try:
        age = datetime.now(UTC) - datetime.fromisoformat(last_at)
        age_days = age.total_seconds() / 86400
    except (ValueError, TypeError):
        age_days = None

    stale = age_days is not None and age_days > 8
    return jsonify({
        "status": "stale" if stale else "ok",
        "last_snapshot_at": last_at,
        "age_days": round(age_days, 1) if age_days is not None else None,
        "message": (
            f"Last snapshot {age_days:.1f} days ago (threshold: 8 days)"
            if age_days is not None else "Could not parse timestamp"
        ),
    })


@blueprint.route("/api/genesis/eval/subsystem-grades")
@_async_route
async def eval_subsystem_grades():
    """Return latest per-subsystem quality grades."""
    from genesis.db.crud import j9_eval
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt._db is None:
        return jsonify({"grades": {}}), 503

    grades = await j9_eval.get_latest_subsystem_grades(rt._db)
    result = {}
    for g in grades:
        result[g["subsystem"]] = {
            "grade": g.get("grade"),
            "score": g.get("score"),
            "factors": g.get("factors", {}),
            "sample_count": g.get("sample_count", 0),
            "period_end": g.get("period_end"),
            "reason": g.get("factors", {}).get("reason"),
        }

    return jsonify({"grades": result})


@blueprint.route("/api/genesis/eval/subsystem-grades/trend")
@_async_route
async def eval_subsystem_grades_trend():
    """Return 12-week trend for each subsystem grade."""
    from genesis.db.crud import j9_eval
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt._db is None:
        return jsonify({"trends": {}}), 503

    subsystems = ["memory", "ego", "procedural", "awareness", "reflection"]
    trends = {}
    for sub in subsystems:
        history = await j9_eval.get_subsystem_grades(
            rt._db, subsystem=sub, period_type="weekly", limit=12,
        )
        history.reverse()  # oldest first for sparkline
        trends[sub] = [
            {
                "period_end": h.get("period_end"),
                "grade": h.get("grade"),
                "score": h.get("score"),
                "sample_count": h.get("sample_count", 0),
            }
            for h in history
        ]

    return jsonify({"trends": trends})
