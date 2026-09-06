"""J-9 eval metrics routes — compounding intelligence dashboard."""

from __future__ import annotations

import logging

from flask import jsonify

from genesis.dashboard._blueprint import _async_route, blueprint

logger = logging.getLogger(__name__)

_DIMENSIONS = (
    "memory", "system", "ego", "cognitive", "procedure",
    "approvals", "goals", "noise", "dev_quality",
)

# Which metric to extract as the "headline" value per dimension
#: Dimensions whose headline metric carries a definition-version marker in its
#: snapshot metrics. A change in the marker is a series break, not a trend.
_HEADLINE_DEFN_KEY = {
    "ego": "approval_rate_defn",
}


def detect_series_break(
    snapshots: list[dict], defn_key: str | None,
) -> str | None:
    """period_end where a headline metric's DEFINITION changes, else None.

    A redefined metric cannot be read as one trend line: the ego's
    approval_rate denominator changed on 2026-09-06, which shrinks the
    denominator and raises the rate, so an unmarked series would show a rise
    caused by nothing but the redefinition. Points are kept and the break is
    named, rather than dropping points and blanking the chart.

    Snapshots must be in chronological order.
    """
    if not defn_key:
        return None
    prev_defn = None
    for i, snap in enumerate(snapshots):
        defn = (snap.get("metrics") or {}).get(defn_key)
        if i > 0 and defn != prev_defn:
            return snap.get("period_end")
        prev_defn = defn
    return None

_HEADLINE_METRIC = {
    "memory": "precision_at_5",
    "system": "composite_score",
    "ego": "approval_rate",
    "cognitive": "delta",
    "procedure": "success_rate",
    "approvals": "user_resolved_rate",
    "goals": "completion_rate",
    "noise": "empty_ego_cycle_pct",
    "dev_quality": "findings_per_pr",
}

# Valence of a RISING headline value per dimension: True = improving,
# False = degrading, None = direction-ambiguous. Drives trend_good (the
# display color) while `trend` stays the factual direction of the value —
# a rising noise metric must never render as improvement.
_HIGHER_IS_BETTER = {
    "memory": True,
    "system": True,
    "ego": True,
    "cognitive": True,
    "procedure": True,
    # more human touch on approval gates = more oversight, not better/worse
    "approvals": None,
    "goals": True,
    "noise": False,
    # fewer findings/PR = better code OR a weaker review — direction-ambiguous
    "dev_quality": None,
}


def _trend_good(dim: str, trend: str) -> bool | None:
    """Map a factual trend direction to a valence for the given dimension.

    Returns None (neutral display) when the dimension's valence is ambiguous
    or the trend is flat/insufficient.
    """
    higher_is_better = _HIGHER_IS_BETTER.get(dim, True)
    if higher_is_better is None or trend not in ("up", "down"):
        return None
    return (trend == "up") == higher_is_better


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
        # A headline metric whose DEFINITION changed cannot be plotted as one
        # line. The ego's approval_rate denominator changed on 2026-09-06
        # (tabled/withdrawn stopped counting as rejections), which shrinks the
        # denominator and raises the rate — so an unmarked sparkline would show
        # a rise caused by nothing but the redefinition. Carry the definition
        # per point and name where it breaks, rather than dropping points and
        # blanking the chart.
        defn_key = _HEADLINE_DEFN_KEY.get(dim)
        series_break_at = detect_series_break(snapshots, defn_key)
        for snap in snapshots:
            metrics = snap.get("metrics", {})
            series.append({
                "period_end": snap.get("period_end"),
                "value": metrics.get(headline_key),
                "sample_count": snap.get("sample_count", 0),
                "definition": metrics.get(defn_key) if defn_key else None,
                "metrics": metrics,
            })

        latest = snapshots[-1] if snapshots else None
        dimensions[dim] = {
            "headline_metric": headline_key,
            "current_value": (
                latest.get("metrics", {}).get(headline_key) if latest else None
            ),
            "series": series,
            # Non-null when this dimension's headline metric was redefined
            # inside the plotted window: the period_end where the new
            # definition starts. Consumers must not read across it as a trend.
            "series_break_at": series_break_at,
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
        data["trend_good"] = _trend_good(_dim, data["trend"])

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
