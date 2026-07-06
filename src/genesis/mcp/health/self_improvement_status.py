"""Outcome Bus soak observability — internal helper.

Provides ``_impl_self_improvement_status``, the Outcome Bus / ego-calibration
readout. As of the LC0 consolidation this is **no longer a standalone MCP tool**:
its content is surfaced as the ``outcome_bus`` section of ``loop_closure_status``
(one self-learning-health surface instead of two). Read-only.

It answers: how much ground-truth has the bus captured (tier/coverage), and how
is the ego's calibration trending?

It reads three existing CRUD surfaces directly — the Outcome Bus ledger
(``outcome_events``) and the measure-only ego calibration snapshots — and does
NOT rank, score, or change any behaviour. There is deliberately no ``worst_at``
weakness ranking here: with the live data still all-positive and no consumer
yet, the ranking semantics belong with the eventual consumer (the bus → 6th
source of capability_map → Phase-3 orchestrator), not baked into a dark tool.
Raw numbers; the human reading them applies judgement.
"""

from __future__ import annotations


async def _impl_self_improvement_status() -> dict:
    import genesis.mcp.health_mcp as health_mcp_mod

    _service = health_mcp_mod._service
    if _service is None or _service._db is None:
        return {"status": "unavailable", "message": "DB not initialized"}

    db = _service._db
    from genesis.db.crud import ego_calibration as cal_crud
    from genesis.db.crud import outcome_events as oe_crud

    tier_counts = await oe_crud.count_by_tier(db)
    signal_type_counts = await oe_crud.count_by_signal_type(db)
    total_events = sum(tier_counts.values())

    # Per-domain T1 ground truth, ALL-TIME (days=None) so the breakdown
    # reconciles with the lifetime tier_counts above. value is strictly 1.0/0.0
    # for execution_outcome rows, so AVG(value) == success rate. Raw counts; no ranking.
    domains_t1 = await oe_crud.aggregate_by_domain(db, tier=1, days=None)
    t1_view = [
        {
            "domain": d["domain"],
            "n": d["n"],
            "success_rate": (
                round(d["avg_value"], 3) if d["avg_value"] is not None else None
            ),
            "positive": d["positive"],
            "negative": d["negative"],
        }
        for d in domains_t1
    ]

    # Measure-only ego calibration (separate store, no cognitive-path reader).
    latest = await cal_crud.get_latest(db, domain="ego")
    if latest is None:
        calibration: dict = {
            "status": "no_data",
            "message": (
                "No ego calibration snapshot yet — computed at 09:00/21:00 once "
                "the Outcome Bus has ego-sourced ground-truth rows "
                "(calibration_pairs requires source='ego' with stated "
                "confidence; T1 volume from other sources doesn't count). "
                "Expected while the ego-proposal loop has no resolved "
                "proposals — not a pipeline fault."
            ),
        }
    else:
        trend = await cal_crud.get_trend(db, domain="ego", limit=10)
        calibration = {
            "status": "ok",
            "ece": latest["ece"],
            "mce": latest["mce"],
            "sample_count": latest["sample_count"],
            "low_confidence": latest["low_confidence"],
            "computed_at": latest["computed_at"],
            "ece_trend": [t["ece"] for t in trend],  # newest first
        }

    return {
        "status": "ok",
        "bus_total_events": total_events,
        # Stringify keys: FastMCP serialises through JSON (which coerces dict
        # keys to strings), so present the same shape a client receives.
        # "1" = ground-truth, "2" = rationale, "3" = coverage.
        "tier_counts": {str(k): v for k, v in tier_counts.items()},
        "signal_type_counts": signal_type_counts,
        "t1_domains": t1_view,
        "ego_calibration": calibration,
        "note": (
            "DARK soak instrument — reading this does NOT change behaviour. T1 "
            "success_rate = AVG(value) over execution_outcome rows (1.0/0.0). No "
            "ranking: raw data for human judgement. T1 feeds capability_map as a "
            "6th source only after the soak (deferred follow-up)."
        ),
    }
