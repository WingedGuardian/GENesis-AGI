"""J-9 weekly eval aggregator — computes snapshots for all 5 dimensions.

Runs on a weekly CronTrigger. Reads eval_events and existing DB tables
to compute metrics for each dimension, stores results in eval_snapshots.

Dimensions:
1. Memory retrieval quality (precision@5, hit rate, MRR, usage rate)
2. System improvement composite (session success, ego acceptance, etc.)
3. Ego proposal quality (approval rate, confidence calibration)
4. Cognitive loop value (recall vs no-recall session comparison)
5. Procedural learning effectiveness (invocation rate, success rate)
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from genesis.db.crud import j9_eval

if TYPE_CHECKING:
    import aiosqlite

logger = logging.getLogger(__name__)


async def run_weekly_aggregation(db: aiosqlite.Connection) -> dict[str, dict]:
    """Compute and store weekly snapshots for all 5 eval dimensions.

    Returns dict of {dimension: metrics} for logging/inspection.
    """
    now = datetime.now(UTC)
    period_end = now.isoformat()
    period_start = (now - timedelta(days=7)).isoformat()

    results: dict[str, dict] = {}

    for name, fn in [
        ("memory", _compute_memory_quality),
        ("system", _compute_system_composite),
        ("ego", _compute_ego_quality),
        ("cognitive", _compute_cognitive_loop),
        ("procedure", _compute_procedural_effectiveness),
    ]:
        try:
            metrics, sample_count = await fn(db, period_start, period_end)
            await j9_eval.insert_snapshot(
                db,
                period_start=period_start,
                period_end=period_end,
                period_type="weekly",
                dimension=name,
                metrics=metrics,
                sample_count=sample_count,
            )
            results[name] = metrics
            logger.info("J9 weekly %s: %d samples", name, sample_count)
        except Exception:
            logger.warning("J9 weekly %s aggregation failed", name, exc_info=True)

    # Composite: trend analysis across prior weeks
    try:
        composite = await _compute_composite_trends(db)
        await j9_eval.insert_snapshot(
            db,
            period_start=period_start,
            period_end=period_end,
            period_type="weekly",
            dimension="composite",
            metrics=composite,
            sample_count=len(results),
        )
        results["composite"] = composite
    except Exception:
        logger.warning("J9 weekly composite failed", exc_info=True)

    return results


# ── Dimension 1: Memory Retrieval Quality ────────────────────────────────────


async def _compute_memory_quality(
    db: aiosqlite.Connection, since: str, until: str,
) -> tuple[dict, int]:
    """Precision@5, hit rate, MRR from recall_relevance events."""
    relevance_events = await j9_eval.get_events(
        db, dimension="memory", event_type="recall_relevance",
        since=since, until=until, limit=5000,
    )

    if not relevance_events:
        return {"precision_at_5": None, "hit_rate": None, "mrr": None,
                "usage_rate": None, "total_recalls": 0}, 0

    # Group by recall_event_id
    by_recall: dict[str, list[dict]] = defaultdict(list)
    for ev in relevance_events:
        m = ev.get("metrics", {})
        rid = m.get("recall_event_id", "")
        if rid:
            by_recall[rid].append(m)

    # Compute per-recall metrics
    precisions = []
    mrrs = []
    hits = 0
    total_recalls = len(by_recall)

    for _rid, memories in by_recall.items():
        # Sort by original position (memory_ids order in recall_fired)
        relevant_count = sum(1 for m in memories if m.get("relevance", 0) >= 0.5)
        precisions.append(relevant_count / max(len(memories), 1))

        # MRR: rank of first relevant result
        found_relevant = False
        for rank, m in enumerate(memories, 1):
            if m.get("relevance", 0) >= 0.5:
                mrrs.append(1.0 / rank)
                found_relevant = True
                hits += 1
                break
        if not found_relevant:
            mrrs.append(0.0)

    # Also check recall_used events for usage rate
    used_events = await j9_eval.get_events(
        db, dimension="memory", event_type="recall_used",
        since=since, until=until, limit=5000,
    )
    total_used = sum(1 for ev in used_events if ev.get("metrics", {}).get("used"))
    total_usage_checked = len(used_events)

    metrics = {
        "precision_at_5": round(sum(precisions) / len(precisions), 4) if precisions else None,
        "hit_rate": round(hits / total_recalls, 4) if total_recalls else None,
        "mrr": round(sum(mrrs) / len(mrrs), 4) if mrrs else None,
        "usage_rate": round(total_used / total_usage_checked, 4) if total_usage_checked else None,
        "total_recalls": total_recalls,
        "total_memories_judged": len(relevance_events),
    }
    return metrics, total_recalls


# ── Dimension 2: System Improvement Composite ────────────────────────────────


async def _compute_system_composite(
    db: aiosqlite.Connection, since: str, until: str,
) -> tuple[dict, int]:
    """Aggregate signals from cc_sessions, ego_proposals, observations, procedures."""
    # Session success rate
    cursor = await db.execute(
        """SELECT
            COUNT(*) as total,
            SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) as completed,
            SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed
        FROM cc_sessions
        WHERE started_at >= ? AND started_at < ?""",
        (since, until),
    )
    row = await cursor.fetchone()
    session_total = row["total"] if row else 0
    session_completed = row["completed"] if row else 0
    session_success_pct = round(session_completed / session_total, 4) if session_total else None

    # Ego acceptance rate
    cursor = await db.execute(
        """SELECT
            COUNT(*) as total,
            SUM(CASE WHEN status = 'approved' OR status = 'executed' THEN 1 ELSE 0 END) as accepted
        FROM ego_proposals
        WHERE created_at >= ? AND created_at < ?
        AND status NOT IN ('pending', 'expired')""",
        (since, until),
    )
    row = await cursor.fetchone()
    ego_total = row["total"] if row else 0
    ego_accepted = row["accepted"] if row else 0
    ego_acceptance_pct = round(ego_accepted / ego_total, 4) if ego_total else None

    # Observation influence rate
    cursor = await db.execute(
        """SELECT
            COUNT(*) as total,
            SUM(influenced_action) as influenced
        FROM observations
        WHERE created_at >= ? AND created_at < ?""",
        (since, until),
    )
    row = await cursor.fetchone()
    obs_total = row["total"] if row else 0
    obs_influenced = row["influenced"] if row else 0
    obs_influence_pct = round(obs_influenced / obs_total, 4) if obs_total else None

    # Procedure mean confidence
    cursor = await db.execute(
        "SELECT AVG(confidence) as avg_conf FROM procedural_memory WHERE deprecated = 0",
    )
    row = await cursor.fetchone()
    proc_mean_conf = round(row["avg_conf"], 4) if row and row["avg_conf"] else None

    # Get memory precision from this week's snapshot (if computed already)
    mem_snapshot = await j9_eval.get_latest_snapshot(db, dimension="memory")
    mem_precision = mem_snapshot["metrics"].get("precision_at_5") if mem_snapshot else None

    # Composite: simple average of available signals (weighted equally)
    signals = [v for v in [session_success_pct, ego_acceptance_pct,
                            obs_influence_pct, proc_mean_conf, mem_precision]
               if v is not None]
    composite = round(sum(signals) / len(signals), 4) if signals else None

    sample_count = session_total + ego_total + obs_total

    metrics = {
        "session_success_pct": session_success_pct,
        "session_total": session_total,
        "ego_acceptance_pct": ego_acceptance_pct,
        "ego_total": ego_total,
        "observation_influence_pct": obs_influence_pct,
        "observation_total": obs_total,
        "procedure_mean_confidence": proc_mean_conf,
        "memory_precision_at5": mem_precision,
        "composite_score": composite,
    }
    return metrics, sample_count


# ── Dimension 3: Ego Proposal Quality ────────────────────────────────────────


async def _compute_ego_quality(
    db: aiosqlite.Connection, since: str, until: str,
) -> tuple[dict, int]:
    """Approval rate, execution success, confidence calibration."""
    cursor = await db.execute(
        """SELECT id, status, confidence, action_type
        FROM ego_proposals
        WHERE created_at >= ? AND created_at < ?""",
        (since, until),
    )
    proposals = [dict(r) for r in await cursor.fetchall()]
    total = len(proposals)

    if not total:
        return {"approval_rate": None, "execution_success_rate": None,
                "confidence_calibration": {}, "total_proposals": 0}, 0

    # Approval rate (resolved proposals only)
    resolved = [p for p in proposals if p["status"] not in ("pending", "expired")]
    approved = [p for p in resolved if p["status"] in ("approved", "executed")]
    approval_rate = round(len(approved) / len(resolved), 4) if resolved else None

    # Execution success rate
    executed = [p for p in proposals if p["status"] == "executed"]
    failed = [p for p in proposals if p["status"] == "failed"]
    exec_total = len(executed) + len(failed)
    exec_success = round(len(executed) / exec_total, 4) if exec_total else None

    # Confidence calibration: bin by confidence decile
    calibration: dict[str, dict] = {}
    for bucket_low in [0.0, 0.2, 0.4, 0.6, 0.8]:
        bucket_high = bucket_low + 0.2
        label = f"{bucket_low:.1f}-{bucket_high:.1f}"
        in_bucket = [p for p in resolved
                     if p.get("confidence") is not None
                     and bucket_low <= p["confidence"] < bucket_high]
        if in_bucket:
            bucket_approved = [p for p in in_bucket
                               if p["status"] in ("approved", "executed")]
            calibration[label] = {
                "count": len(in_bucket),
                "success_rate": round(len(bucket_approved) / len(in_bucket), 4),
            }

    metrics = {
        "approval_rate": approval_rate,
        "execution_success_rate": exec_success,
        "confidence_calibration": calibration,
        "total_proposals": total,
        "total_resolved": len(resolved),
    }
    return metrics, total


# ── Dimension 4: Cognitive Loop Value ────────────────────────────────────────


async def _compute_cognitive_loop(
    db: aiosqlite.Connection, since: str, until: str,
) -> tuple[dict, int]:
    """Compare sessions with proactive recall vs without."""
    # Get all sessions in the window
    cursor = await db.execute(
        """SELECT id, status, started_at, completed_at
        FROM cc_sessions
        WHERE started_at >= ? AND started_at < ?""",
        (since, until),
    )
    sessions = [dict(r) for r in await cursor.fetchall()]

    if not sessions:
        return {"sessions_with_recall": 0, "sessions_without_recall": 0,
                "success_rate_with": None, "success_rate_without": None,
                "delta": None}, 0

    # Find which sessions had recall_fired events
    recall_events = await j9_eval.get_events(
        db, dimension="memory", event_type="recall_fired",
        since=since, until=until, limit=5000,
    )
    sessions_with_recall = {ev.get("session_id") for ev in recall_events
                            if ev.get("session_id")}

    with_recall = [s for s in sessions if s["id"] in sessions_with_recall]
    without_recall = [s for s in sessions if s["id"] not in sessions_with_recall]

    def _success_rate(session_list: list[dict]) -> float | None:
        if not session_list:
            return None
        completed = sum(1 for s in session_list if s["status"] == "completed")
        return round(completed / len(session_list), 4)

    rate_with = _success_rate(with_recall)
    rate_without = _success_rate(without_recall)

    delta = None
    if rate_with is not None and rate_without is not None:
        delta = round(rate_with - rate_without, 4)

    metrics = {
        "sessions_with_recall": len(with_recall),
        "sessions_without_recall": len(without_recall),
        "success_rate_with": rate_with,
        "success_rate_without": rate_without,
        "delta": delta,
    }
    return metrics, len(sessions)


# ── Dimension 5: Procedural Learning Effectiveness ───────────────────────────


async def _compute_procedural_effectiveness(
    db: aiosqlite.Connection, since: str, until: str,
) -> tuple[dict, int]:
    """Invocation frequency, success rate, confidence calibration."""
    # Invocation events
    invocations = await j9_eval.get_events(
        db, dimension="procedure", event_type="procedure_invoked",
        since=since, until=until, limit=1000,
    )
    invocation_count = len(invocations)

    # Outcome events
    outcomes = await j9_eval.get_events(
        db, dimension="procedure", event_type="procedure_outcome",
        since=since, until=until, limit=1000,
    )
    successes = sum(1 for o in outcomes if o.get("metrics", {}).get("success"))
    outcome_total = len(outcomes)
    success_rate = round(successes / outcome_total, 4) if outcome_total else None

    # Overall procedure stats
    cursor = await db.execute(
        """SELECT COUNT(*) as total, AVG(confidence) as avg_conf
        FROM procedural_memory WHERE deprecated = 0""",
    )
    row = await cursor.fetchone()
    total_procedures = row["total"] if row else 0
    mean_confidence = round(row["avg_conf"], 4) if row and row["avg_conf"] else None

    # Tier distribution
    cursor = await db.execute(
        """SELECT activation_tier, COUNT(*) as cnt
        FROM procedural_memory WHERE deprecated = 0
        GROUP BY activation_tier""",
    )
    tiers = {r["activation_tier"]: r["cnt"] for r in await cursor.fetchall()}

    # Confidence calibration from outcome events
    calibration: dict[str, dict] = {}
    for o in outcomes:
        m = o.get("metrics", {})
        conf = m.get("confidence_after")
        success = m.get("success", False)
        if conf is not None:
            bucket = f"{int(conf * 10) / 10:.1f}"
            if bucket not in calibration:
                calibration[bucket] = {"total": 0, "success": 0}
            calibration[bucket]["total"] += 1
            if success:
                calibration[bucket]["success"] += 1
    for bucket in calibration:
        calibration[bucket]["rate"] = round(
            calibration[bucket]["success"] / calibration[bucket]["total"], 4,
        )

    metrics = {
        "invocation_count": invocation_count,
        "outcome_count": outcome_total,
        "success_rate": success_rate,
        "mean_confidence": mean_confidence,
        "total_procedures": total_procedures,
        "tier_distribution": tiers,
        "confidence_calibration": calibration,
    }
    return metrics, invocation_count + outcome_total


# ── Composite Trends ─────────────────────────────────────────────────────────


async def _compute_composite_trends(db: aiosqlite.Connection) -> dict:
    """Compute slopes across prior weekly snapshots for GO/NO-GO criteria."""
    # Get up to 12 weeks of history per dimension
    dimensions = ["memory", "system", "ego", "cognitive", "procedure"]
    trends: dict[str, list[float]] = {}

    for dim in dimensions:
        snapshots = await j9_eval.get_snapshots(
            db, dimension=dim, period_type="weekly", limit=12,
        )
        snapshots.reverse()  # oldest first
        trends[dim] = _extract_trend_values(dim, snapshots)

    # Compute slopes via simple linear regression
    slopes: dict[str, float | None] = {}
    for dim, values in trends.items():
        slopes[f"{dim}_slope"] = _simple_slope(values) if len(values) >= 2 else None

    # GO/NO-GO criteria check
    go_count = 0
    if slopes.get("memory_slope") is not None and slopes["memory_slope"] > 0:
        go_count += 1
    if slopes.get("system_slope") is not None and slopes["system_slope"] > 0:
        go_count += 1
    if slopes.get("ego_slope") is not None and slopes["ego_slope"] > 0:
        go_count += 1
    if slopes.get("cognitive_slope") is not None and slopes["cognitive_slope"] > 0:
        go_count += 1

    return {
        **slopes,
        "weeks_of_data": max(len(v) for v in trends.values()) if trends else 0,
        "go_criteria_met": go_count,
        "go_ready": go_count >= 2,
    }


def _extract_trend_values(dimension: str, snapshots: list[dict]) -> list[float]:
    """Extract the key metric for trend tracking from each snapshot."""
    key_metric = {
        "memory": "precision_at_5",
        "system": "composite_score",
        "ego": "approval_rate",
        "cognitive": "delta",
        "procedure": "success_rate",
    }.get(dimension, "composite_score")

    values = []
    for s in snapshots:
        m = s.get("metrics", {})
        v = m.get(key_metric)
        if v is not None:
            values.append(float(v))
    return values


def _simple_slope(values: list[float]) -> float | None:
    """Simple linear regression slope over equally-spaced values."""
    n = len(values)
    if n < 2:
        return None
    x_mean = (n - 1) / 2.0
    y_mean = sum(values) / n
    numerator = sum((i - x_mean) * (v - y_mean) for i, v in enumerate(values))
    denominator = sum((i - x_mean) ** 2 for i in range(n))
    return round(numerator / denominator, 6) if denominator else None
