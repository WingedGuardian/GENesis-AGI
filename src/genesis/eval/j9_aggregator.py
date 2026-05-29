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

    # ── Per-subsystem grades ──────────────────────────────────────────────
    subsystem_results: dict[str, dict] = {}
    for sub_name, grade_fn in [
        ("memory", _grade_memory),
        ("ego", _grade_ego),
        ("procedural", _grade_procedural),
        ("awareness", _grade_awareness),
        ("reflection", _grade_reflection),
    ]:
        try:
            grade_info = await grade_fn(db, period_start, period_end, results)
            await j9_eval.insert_subsystem_grade(
                db,
                period_start=period_start,
                period_end=period_end,
                period_type="weekly",
                subsystem=sub_name,
                grade=grade_info["grade"],
                score=grade_info["score"],
                factors=grade_info["factors"],
                sample_count=grade_info["sample_count"],
            )
            subsystem_results[sub_name] = grade_info
            logger.info(
                "J9 subsystem %s: grade=%s score=%s (%d samples)",
                sub_name, grade_info["grade"], grade_info["score"],
                grade_info["sample_count"],
            )
        except Exception:
            logger.warning("J9 subsystem %s grading failed", sub_name, exc_info=True)

    results["subsystem_grades"] = subsystem_results

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


# ── Per-Subsystem Grading ───────────────────────────────────────────────────

# Minimum samples required per subsystem before issuing a letter grade.
_MIN_SAMPLES = {"memory": 10, "ego": 5, "procedural": 5,
                "awareness": 20, "reflection": 10}


def _score_to_grade(score: float | None) -> str | None:
    """Convert a 0-100 score to a letter grade. None if insufficient data."""
    if score is None:
        return None
    if score >= 90:
        return "A"
    if score >= 80:
        return "B"
    if score >= 70:
        return "C"
    if score >= 60:
        return "D"
    return "F"


async def _grade_memory(
    db: aiosqlite.Connection,
    since: str,
    until: str,
    dimension_results: dict[str, dict],
) -> dict:
    """Grade memory subsystem from dimension 1 metrics.

    Factors: precision@5 (40%), MRR (30%), usage_rate (30%).
    All factors are 0.0-1.0 natively, scaled to 0-100.
    """
    metrics = dimension_results.get("memory", {})
    p5 = metrics.get("precision_at_5")
    mrr = metrics.get("mrr")
    usage = metrics.get("usage_rate")
    total = metrics.get("total_recalls", 0)

    factors = {"precision_at_5": p5, "mrr": mrr, "usage_rate": usage,
               "total_recalls": total}

    available = [(p5, 0.4), (mrr, 0.3), (usage, 0.3)]
    valid = [(v, w) for v, w in available if v is not None]

    if not valid or total < _MIN_SAMPLES["memory"]:
        return {"grade": None, "score": None, "factors": factors,
                "sample_count": total,
                "reason": f"insufficient data ({total} recalls, need {_MIN_SAMPLES['memory']})"}

    # Re-normalize weights if some factors are missing
    total_weight = sum(w for _, w in valid)
    score = sum(v * w / total_weight for v, w in valid) * 100

    return {"grade": _score_to_grade(score), "score": round(score, 1),
            "factors": factors, "sample_count": total}


async def _grade_ego(
    db: aiosqlite.Connection,
    since: str,
    until: str,
    dimension_results: dict[str, dict],
) -> dict:
    """Grade ego subsystem from dimension 3 metrics.

    Factors: approval_rate (40%), execution_success (30%),
    confidence_calibration_accuracy (30%).
    """
    metrics = dimension_results.get("ego", {})
    approval = metrics.get("approval_rate")
    exec_success = metrics.get("execution_success_rate")
    total = metrics.get("total_proposals", 0)

    # Confidence calibration accuracy: how well does stated confidence
    # predict actual approval? Perfect calibration = 1.0.
    cal = metrics.get("confidence_calibration", {})
    cal_errors = []
    for bucket_label, bucket_data in cal.items():
        # Expected success = midpoint of bucket range
        parts = bucket_label.split("-")
        if len(parts) == 2:
            try:
                expected = (float(parts[0]) + float(parts[1])) / 2
            except (ValueError, TypeError):
                continue
            actual = bucket_data.get("success_rate", 0)
            cal_errors.append(abs(expected - actual))
    cal_accuracy = 1.0 - (sum(cal_errors) / len(cal_errors)) if cal_errors else None

    factors = {"approval_rate": approval, "execution_success_rate": exec_success,
               "calibration_accuracy": cal_accuracy,
               "total_proposals": total}

    available = [(approval, 0.4), (exec_success, 0.3), (cal_accuracy, 0.3)]
    valid = [(v, w) for v, w in available if v is not None]

    if not valid or total < _MIN_SAMPLES["ego"]:
        return {"grade": None, "score": None, "factors": factors,
                "sample_count": total,
                "reason": f"insufficient data ({total} proposals, need {_MIN_SAMPLES['ego']})"}

    total_weight = sum(w for _, w in valid)
    score = sum(v * w / total_weight for v, w in valid) * 100

    return {"grade": _score_to_grade(score), "score": round(score, 1),
            "factors": factors, "sample_count": total}


async def _grade_procedural(
    db: aiosqlite.Connection,
    since: str,
    until: str,
    dimension_results: dict[str, dict],
) -> dict:
    """Grade procedural subsystem from dimension 5 metrics.

    Factors: success_rate (50%), mean_confidence (25%),
    invocation_activity (25% — normalized by total procedures).
    """
    metrics = dimension_results.get("procedure", {})
    success = metrics.get("success_rate")
    confidence = metrics.get("mean_confidence")
    invocations = metrics.get("invocation_count", 0)
    total_procs = metrics.get("total_procedures", 1)

    # Invocation activity: ratio of invocations to total procedures.
    # >1.0 per procedure per week = healthy, cap at 1.0 for scoring.
    activity = min(invocations / max(total_procs, 1), 1.0) if invocations else 0.0

    factors = {"success_rate": success, "mean_confidence": confidence,
               "invocation_count": invocations, "activity_ratio": round(activity, 4),
               "total_procedures": total_procs}

    available = [(success, 0.5), (confidence, 0.25), (activity, 0.25)]
    valid = [(v, w) for v, w in available if v is not None]

    if not valid or invocations < _MIN_SAMPLES["procedural"]:
        return {"grade": None, "score": None, "factors": factors,
                "sample_count": invocations,
                "reason": f"insufficient data ({invocations} invocations, need {_MIN_SAMPLES['procedural']})"}

    total_weight = sum(w for _, w in valid)
    score = sum(v * w / total_weight for v, w in valid) * 100

    return {"grade": _score_to_grade(score), "score": round(score, 1),
            "factors": factors, "sample_count": invocations}


async def _grade_awareness(
    db: aiosqlite.Connection,
    since: str,
    until: str,
    _dimension_results: dict[str, dict],
) -> dict:
    """Grade awareness subsystem from awareness_ticks data.

    Factors: tick_regularity (30%), classification_rate (40%),
    depth_balance (30%).
    """
    # Total ticks in period
    cursor = await db.execute(
        "SELECT COUNT(*) as cnt FROM awareness_ticks "
        "WHERE created_at >= ? AND created_at < ?",
        (since, until),
    )
    row = await cursor.fetchone()
    total_ticks = row["cnt"] if row else 0

    # Classified ticks (non-null classified_depth)
    cursor = await db.execute(
        "SELECT classified_depth, COUNT(*) as cnt FROM awareness_ticks "
        "WHERE created_at >= ? AND created_at < ? "
        "AND classified_depth IS NOT NULL "
        "GROUP BY classified_depth",
        (since, until),
    )
    depth_rows = await cursor.fetchall()
    depth_dist = {r["classified_depth"]: r["cnt"] for r in depth_rows}
    classified_count = sum(depth_dist.values())

    # Tick regularity: expect ~2000+/week (5-min ticks, 2016 per week).
    # Score as ratio to expected, capped at 1.0.
    expected_ticks = 2016  # 7 * 24 * 60 / 5
    tick_regularity = min(total_ticks / expected_ticks, 1.0) if total_ticks else 0.0

    # Classification rate: what fraction of ticks got classified
    classification_rate = classified_count / total_ticks if total_ticks else 0.0

    # Depth balance: entropy-like measure of depth distribution.
    # Perfect balance (all 4 depths equal) = 1.0. All one depth = 0.0.
    depth_balance = 0.0
    if classified_count > 0 and len(depth_dist) > 1:
        import math
        probs = [c / classified_count for c in depth_dist.values()]
        max_entropy = math.log(4)  # 4 depth levels
        entropy = -sum(p * math.log(p) for p in probs if p > 0)
        depth_balance = entropy / max_entropy if max_entropy else 0.0

    factors = {"tick_regularity": round(tick_regularity, 4),
               "classification_rate": round(classification_rate, 4),
               "depth_balance": round(depth_balance, 4),
               "total_ticks": total_ticks,
               "classified_count": classified_count,
               "depth_distribution": depth_dist}

    if total_ticks < _MIN_SAMPLES["awareness"]:
        return {"grade": None, "score": None, "factors": factors,
                "sample_count": total_ticks,
                "reason": f"insufficient data ({total_ticks} ticks, need {_MIN_SAMPLES['awareness']})"}

    score = (tick_regularity * 0.3 + classification_rate * 0.4
             + depth_balance * 0.3) * 100

    return {"grade": _score_to_grade(score), "score": round(score, 1),
            "factors": factors, "sample_count": total_ticks}


async def _grade_reflection(
    db: aiosqlite.Connection,
    since: str,
    until: str,
    _dimension_results: dict[str, dict],
) -> dict:
    """Grade reflection subsystem from observations data.

    Factors: observation_volume (25%), influence_rate (50%),
    type_diversity (25%).
    """
    # Total observations in period
    cursor = await db.execute(
        """SELECT COUNT(*) as total,
                  SUM(CASE WHEN influenced_action = 1 THEN 1 ELSE 0 END) as influenced
           FROM observations
           WHERE created_at >= ? AND created_at < ?""",
        (since, until),
    )
    row = await cursor.fetchone()
    total_obs = row["total"] if row else 0
    influenced = row["influenced"] if row else 0

    # Influence rate: fraction of observations that led to action
    influence_rate = influenced / total_obs if total_obs else 0.0

    # Observation volume: expect 50+/week for a healthy system.
    # Score as ratio to expected, capped at 1.0.
    volume_score = min(total_obs / 50, 1.0) if total_obs else 0.0

    # Type diversity: how many distinct observation types
    cursor = await db.execute(
        "SELECT COUNT(DISTINCT type) as type_count FROM observations "
        "WHERE created_at >= ? AND created_at < ?",
        (since, until),
    )
    row = await cursor.fetchone()
    type_count = row["type_count"] if row else 0
    # Expect 3+ types for healthy diversity, cap at 1.0
    type_diversity = min(type_count / 3, 1.0) if type_count else 0.0

    factors = {"observation_volume": total_obs, "volume_score": round(volume_score, 4),
               "influence_rate": round(influence_rate, 4),
               "type_diversity": round(type_diversity, 4),
               "type_count": type_count, "influenced_count": influenced}

    if total_obs < _MIN_SAMPLES["reflection"]:
        return {"grade": None, "score": None, "factors": factors,
                "sample_count": total_obs,
                "reason": f"insufficient data ({total_obs} observations, need {_MIN_SAMPLES['reflection']})"}

    score = (volume_score * 0.25 + influence_rate * 0.5
             + type_diversity * 0.25) * 100

    return {"grade": _score_to_grade(score), "score": round(score, 1),
            "factors": factors, "sample_count": total_obs}
