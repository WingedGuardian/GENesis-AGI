"""J-9 eval status MCP tool — quick overview of eval data collection progress."""

from __future__ import annotations

import logging

from genesis.mcp.health import mcp

logger = logging.getLogger(__name__)


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

    # Get latest snapshot per dimension
    latest_snapshots: dict[str, dict | None] = {}
    for dim in [*dimensions, "composite"]:
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

    return {
        "event_counts": event_counts,
        "total_events": sum(event_counts.values()),
        "latest_snapshots": latest_snapshots,
        "go_status": go_status,
    }


@mcp.tool()
async def j9_eval_status() -> dict:
    """J-9 paper eval progress: event counts, latest snapshots, GO/NO-GO status."""
    return await _impl_j9_eval_status()
