"""Ego calibration status MCP tool — does the ego's confidence track reality?

Read-only surface over ``ego_calibration_snapshots``. This is the user-facing
half of the measure-only ego calibration: it reports whether the ego's stated
confidence matches actual outcomes ("says 90%, right 82%"), the ECE, and the
trend over time. It does NOT change ego behaviour (self-correction is a separate,
flagged future PR) and never writes anything.
"""

from __future__ import annotations

import logging

from genesis.mcp.health import mcp

logger = logging.getLogger(__name__)


async def _impl_ego_calibration_status(domain: str = "ego") -> dict:
    import genesis.mcp.health_mcp as health_mcp_mod

    _service = health_mcp_mod._service
    if _service is None or _service._db is None:
        return {"status": "unavailable", "message": "DB not initialized"}

    db = _service._db
    from genesis.db.crud import ego_calibration as cal_crud

    latest = await cal_crud.get_latest(db, domain=domain)
    if latest is None:
        return {
            "status": "no_data",
            "message": (
                f"No calibration snapshots yet for domain={domain!r}. Computed "
                f"at 09:00/21:00 once the Outcome Bus has {domain}-sourced "
                f"ground-truth rows (calibration_pairs requires "
                f"source={domain!r} with stated confidence; T1 volume from "
                f"other sources doesn't count). Expected while that source's "
                f"proposal loop has no resolved, confidence-stated outcomes."
            ),
        }

    trend = await cal_crud.get_trend(db, domain=domain, limit=30)
    curve = latest.get("curve", [])
    curve_readable = [
        f"says ~{c['predicted_confidence']:.0%} → right "
        f"{c['actual_success_rate']:.0%} (n={c['sample_count']})"
        for c in curve
    ]
    # MCE is the worst single bucket and can be inflated by a thin (low-n) bucket.
    note = None
    if latest["mce"] > 2 * latest["ece"]:
        note = "MCE reflects the worst single bucket — check per-bucket n for thin (noisy) bins."

    return {
        "status": "ok",
        "domain": domain,
        "ece": latest["ece"],
        "mce": latest["mce"],
        "sample_count": latest["sample_count"],
        "bucket_count": latest["bucket_count"],
        "low_confidence": latest["low_confidence"],
        "computed_at": latest["computed_at"],
        "curve": curve,
        "curve_readable": curve_readable,
        "ece_trend": [t["ece"] for t in trend],  # newest first
        "note": note,
    }


@mcp.tool()
async def ego_calibration_status(domain: str = "ego") -> dict:
    """How well-calibrated is the ego's confidence?

    Reports whether the ego's stated confidence tracks actual outcomes
    ("says 90%, right 82%"), the Expected Calibration Error (ECE, lower=better),
    the per-confidence-bucket curve, and the ECE trend over time. Measure-only:
    reading this does NOT change ego behaviour. ``low_confidence=true`` means the
    estimate is statistically thin (few samples/buckets) — read it with caution.
    """
    return await _impl_ego_calibration_status(domain)
