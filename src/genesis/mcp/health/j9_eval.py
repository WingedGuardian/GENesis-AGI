"""J-9 eval status MCP tool — quick overview of eval data collection progress."""

from __future__ import annotations

import logging

from genesis.mcp.health import mcp

logger = logging.getLogger(__name__)

# Metric keys pulled from each weekly memory snapshot into the retrieval trend.
# pool_* land only from WS2-0 onward, so every key is read with .get → None on
# older snapshots (the series shape stays stable, gaps are honest nulls).
_TREND_METRIC_KEYS: tuple[str, ...] = (
    "precision_at_5",
    "precision_at_3",
    "hit_rate",
    "mrr",
    "usage_rate",
    "total_recalls",
    "pool_episodic_total",
    "pool_episodic_retrievable",
    "pool_episodic_embedded",
    "pool_knowledge_units_total",
    "pool_memory_links_total",
)


def _retrieval_trend(mem_snapshots: list[dict]) -> list[dict]:
    """Map weekly memory snapshots (newest-first) → a chronological trend series.

    Reads every metric with ``.get`` so pre-WS2-0 snapshots (no ``pool_*``)
    surface as explicit ``None`` rather than a fabricated value or a KeyError.
    """
    trend: list[dict] = []
    for snap in reversed(mem_snapshots):  # DESC → chronological
        metrics = snap.get("metrics", {}) or {}
        row = {"period_end": snap.get("period_end")}
        for key in _TREND_METRIC_KEYS:
            row[key] = metrics.get(key)
        trend.append(row)
    return trend


async def _impl_j9_eval_status() -> dict:
    """Get J-9 eval collection status across all 5 dimensions."""
    import genesis.mcp.health_mcp as health_mcp_mod

    _service = health_mcp_mod._service
    if _service is None or _service._db is None:
        return {"status": "unavailable", "message": "DB not initialized"}

    db = _service._db

    from genesis.db.crud import j9_eval

    # Count events by dimension
    dimensions = ["memory", "ego", "procedure", "cognitive", "system"]
    event_counts: dict[str, int] = {}
    for dim in dimensions:
        event_counts[dim] = await j9_eval.count_events(db, dimension=dim)

    # Get latest snapshot per dimension. approvals/goals/noise are
    # snapshot-only dimensions (no eval_events), so they appear here but
    # not in event_counts above.
    latest_snapshots: dict[str, dict | None] = {}
    for dim in [*dimensions, "composite", "approvals", "goals", "noise"]:
        snap = await j9_eval.get_latest_snapshot(db, dimension=dim)
        if snap:
            latest_snapshots[dim] = {
                "period_end": snap["period_end"],
                "sample_count": snap["sample_count"],
                "metrics": snap.get("metrics", {}),
            }
        else:
            latest_snapshots[dim] = None

    # GO/NO-GO status from composite
    composite = latest_snapshots.get("composite")
    go_status = None
    if composite and composite.get("metrics"):
        go_status = {
            "criteria_met": composite["metrics"].get("go_criteria_met", 0),
            "go_ready": composite["metrics"].get("go_ready", False),
            "weeks_of_data": composite["metrics"].get("weeks_of_data", 0),
        }

    # Retrieval-efficacy trend: last 12 weekly memory snapshots, chronological,
    # so quality (precision@k/hit_rate/MRR) is readable against pool size.
    mem_snaps = await j9_eval.get_snapshots(
        db,
        dimension="memory",
        period_type="weekly",
        limit=12,
    )
    retrieval_trend = _retrieval_trend(mem_snaps)

    return {
        "event_counts": event_counts,
        "total_events": sum(event_counts.values()),
        "latest_snapshots": latest_snapshots,
        "go_status": go_status,
        "retrieval_trend": retrieval_trend,
    }


@mcp.tool()
async def j9_eval_status() -> dict:
    """J-9 paper eval progress: event counts, latest snapshots, GO/NO-GO status."""
    return await _impl_j9_eval_status()
