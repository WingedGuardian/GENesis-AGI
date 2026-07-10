"""J-9 weekly eval aggregator — computes snapshots for all dimensions.

Runs on a weekly CronTrigger. Reads eval_events and existing DB tables
to compute metrics for each dimension, stores results in eval_snapshots.

Dimensions:
1. Memory retrieval quality (precision@5, precision@3, hit rate, MRR, usage rate)
2. System improvement composite (session success, ego acceptance, etc.)
3. Ego proposal quality (approval rate, confidence calibration)
4. Cognitive loop value (recall vs no-recall session comparison)
5. Procedural learning effectiveness (invocation rate, success rate)
6. Cognitive drift (Phase 7, dark): dissent-rate + proposal-diversity
7. Approvals (WS-1 A2): approval-gate resolution throughput
8. Goals (WS-1 A2, scaffold): user_goals status ratios + completion rate
9. Noise/passivity (WS-1 A2): loop-closure leaks + empty-ego-cycle rate
10. Dev quality (component g): PR-review findings + code-audit backlog +
    edit-failure rate
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from genesis.db.crud import ego as ego_crud
from genesis.db.crud import j9_eval

if TYPE_CHECKING:
    import aiosqlite

logger = logging.getLogger(__name__)


async def _compute_cognitive_drift(
    db: aiosqlite.Connection, period_start: str, period_end: str,
) -> tuple[dict, int]:
    """Dark drift metrics over ego_proposals (Phase 7 anti-overfitting baseline).

    - dissent_rate: realist critic engagement = (amend + reject) / proposals with
      a verdict. A FALLING rate is an overfitting alarm (the ego stops being
      challenged / only proposes pre-vetted-safe work).
    - alternative_rate: fraction of proposals that recorded a non-empty alternative.
    - diversity_entropy: normalized Shannon entropy over action_type (mirrors the
      j9 ``type_diversity`` convention). Falling = proposals collapsing/formulaic.

    DARK: no cognitive path reads this; it establishes the baseline the future
    personality-drift gates need. Self-silencing + exploration-floor are deferred
    (statistically underpowered until more T1 negatives accrue).
    """
    import math
    from collections import Counter

    rows = await ego_crud.get_proposals_for_drift(
        db, start=period_start, end=period_end,
    )
    total = len(rows)
    if total == 0:
        return {
            "dissent_rate": None, "alternative_rate": None,
            "diversity_entropy": None, "n_proposals": 0,
        }, 0

    verdicts = [r["realist_verdict"] for r in rows if r["realist_verdict"]]
    dissent = sum(1 for v in verdicts if v in ("amend", "reject"))
    dissent_rate = (dissent / len(verdicts)) if verdicts else None

    alt = sum(1 for r in rows if (r["alternatives"] or "").strip())

    counts = Counter(r["action_type"] for r in rows)
    k = len(counts)
    if k <= 1:
        entropy = 0.0
    else:
        h = -sum((c / total) * math.log(c / total) for c in counts.values())
        entropy = h / math.log(k)  # normalize to [0, 1]

    metrics = {
        "dissent_rate": round(dissent_rate, 4) if dissent_rate is not None else None,
        "alternative_rate": round(alt / total, 4),
        "diversity_entropy": round(entropy, 4),
        "distinct_action_types": k,
        "n_proposals": total,
        "n_with_verdict": len(verdicts),
    }
    return metrics, total


async def run_weekly_aggregation(db: aiosqlite.Connection) -> dict[str, dict]:
    """Compute and store weekly snapshots for all eval dimensions.

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
        ("cognitive_drift", _compute_cognitive_drift),
        ("approvals", _compute_approvals),
        ("goals", _compute_goal_completion),
        ("noise", _compute_noise_passivity),
        ("dev_quality", _compute_dev_quality),
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


def _dedupe_by_pair(events: list[dict]) -> list[dict]:
    """Keep one event per (recall_event_id, memory_id) pair.

    Defends the metrics against duplicate relevance/used events (e.g. produced
    by a batch that re-judged the same window before checkpointing existed).
    Keeps the first occurrence — callers pass timestamp-DESC events, so that is
    the most recent judgment. Events missing either id are kept as-is.
    """
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []
    for ev in events:
        m = ev.get("metrics", {})
        rid = m.get("recall_event_id")
        mid = m.get("memory_id")
        if rid and mid:
            key = (rid, mid)
            if key in seen:
                continue
            seen.add(key)
        out.append(ev)
    return out


async def _recall_entrenchment(
    db: aiosqlite.Connection, since: str, until: str,
) -> dict:
    """MEM-005: weekly aggregation of the activation-entrenchment signal.

    Reads ``recall_fired`` events (a separate read path from the relevance-based
    quality metrics) and averages the per-recall ``entrenchment_corr`` (rank
    correlation between a result's retrieval frequency and its final score),
    plus mean retrieval count and mean recalled-memory age. Monitor-only (D7):
    surfaced in the memory snapshot so a sustained rise can be spotted; it never
    feeds the quality grade.
    """
    fired = await j9_eval.get_events(
        db, dimension="memory", event_type="recall_fired",
        since=since, until=until, limit=5000,
    )
    corrs = [m["entrenchment_corr"] for ev in fired
             if (m := ev.get("metrics", {})).get("entrenchment_corr") is not None]
    ret_counts = [ev.get("metrics", {})["mean_retrieved_count"] for ev in fired
                  if ev.get("metrics", {}).get("mean_retrieved_count") is not None]
    ages = [ev.get("metrics", {})["mean_age_days"] for ev in fired
            if ev.get("metrics", {}).get("mean_age_days") is not None]
    return {
        "entrenchment_corr_mean": round(sum(corrs) / len(corrs), 4) if corrs else None,
        "entrenchment_mean_retrieved_count": (
            round(sum(ret_counts) / len(ret_counts), 2) if ret_counts else None),
        "entrenchment_mean_age_days": (
            round(sum(ages) / len(ages), 1) if ages else None),
        "entrenchment_sample": len(corrs),
    }


async def _compute_memory_quality(
    db: aiosqlite.Connection, since: str, until: str,
) -> tuple[dict, int]:
    """Precision@5, hit rate, MRR from recall_relevance events.

    Also folds in the MEM-005 entrenchment aggregation (separate read path over
    recall_fired) as distinct ``entrenchment_*`` keys — monitor-only, not part
    of the quality grade.
    """
    entrenchment = await _recall_entrenchment(db, since, until)
    relevance_events = await j9_eval.get_events(
        db, dimension="memory", event_type="recall_relevance",
        since=since, until=until, limit=5000,
    )
    # Defensive dedup: duplicate (recall, memory) judgments must not skew the
    # average (pre-checkpoint batches could emit the same pair multiple times).
    relevance_events = _dedupe_by_pair(relevance_events)

    if not relevance_events:
        return {"precision_at_5": None, "precision_at_3": None,
                "precision_at_3_recalls": 0, "judge_prompt_versions": [],
                "hit_rate": None, "mrr": None,
                "usage_rate": None, "total_recalls": 0, **entrenchment}, 0

    # Group by recall_event_id
    by_recall: dict[str, list[dict]] = defaultdict(list)
    for ev in relevance_events:
        m = ev.get("metrics", {})
        rid = m.get("recall_event_id", "")
        if rid:
            by_recall[rid].append(m)

    # Compute per-recall metrics
    precisions = []
    precisions_at_3 = []
    mrrs = []
    hits = 0
    total_recalls = len(by_recall)

    for _rid, memories in by_recall.items():
        # Sort by retrieval rank (the memory's memory_ids[:5] position, persisted
        # in each event's metrics by j9_batch). Events arrive in concurrent-insert /
        # timestamp-DESC order, so list order is NOT the rank — without this sort
        # MRR is meaningless. Pre-fix historical events lack a rank → sort last.
        memories.sort(key=lambda m: m.get("rank", 1 << 30))
        # Defensive top-5 cap: j9_batch judges memory_ids[:5], so post-dedup
        # this is a no-op on well-formed data — but a malformed emitter must
        # not inflate the "@5" denominator beyond 5.
        memories = memories[:5]
        relevant_count = sum(1 for m in memories if m.get("relevance", 0) >= 0.5)
        # Precision among judged (top-k of what was judged), NOT a hard-k
        # denominator — preserves the historical precision_at_5 series.
        precisions.append(relevant_count / max(len(memories), 1))

        # precision@3: only meaningful when every judged memory carries a real
        # rank — for unranked pre-fix events the [:3] slice would be arbitrary.
        # Denominator = min(3, judged); recalls skipped here are visible via
        # precision_at_3_recalls vs total_recalls.
        if all("rank" in m for m in memories):
            top3 = memories[:3]
            relevant_top3 = sum(1 for m in top3 if m.get("relevance", 0) >= 0.5)
            precisions_at_3.append(relevant_top3 / max(len(top3), 1))

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
    used_events = _dedupe_by_pair(used_events)
    total_used = sum(1 for ev in used_events if ev.get("metrics", {}).get("used"))
    total_usage_checked = len(used_events)

    # Judge-prompt versions seen in-window: a change here is a series break
    # (precision@k is judge-rated; a reworded judge prompt shifts the series
    # without any retrieval change). Pre-versioning events → "unversioned".
    judge_prompt_versions = sorted({
        (ev.get("metrics", {}) or {}).get("judge_prompt_version") or "unversioned"
        for ev in relevance_events
    })

    metrics = {
        "precision_at_5": round(sum(precisions) / len(precisions), 4) if precisions else None,
        "precision_at_3": round(sum(precisions_at_3) / len(precisions_at_3), 4)
        if precisions_at_3 else None,
        # Coverage: recalls that qualified for the @3 metric (fully ranked)
        # vs total_recalls — thin coverage must be visible, not hidden.
        "precision_at_3_recalls": len(precisions_at_3),
        "judge_prompt_versions": judge_prompt_versions,
        "hit_rate": round(hits / total_recalls, 4) if total_recalls else None,
        "mrr": round(sum(mrrs) / len(mrrs), 4) if mrrs else None,
        "usage_rate": round(total_used / total_usage_checked, 4) if total_usage_checked else None,
        "total_recalls": total_recalls,
        # Count only memories that actually fed the metrics (grouped under a
        # recall_event_id) — not orphan events that contribute nothing.
        "total_memories_judged": sum(len(v) for v in by_recall.values()),
        # MEM-005 entrenchment signal (monitor-only, distinct from the grade).
        **entrenchment,
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
    counts = await ego_crud.get_acceptance_counts(db, start=since, end=until)
    ego_total = counts["total"] if counts else 0
    ego_accepted = counts["accepted"] if counts else 0
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

    # Procedure mean confidence — VALIDATED procedures only. Draft
    # candidates (extracted at confidence ≈ 0, never invoked) measure extraction
    # volume, not knowledge quality; including them tanked this composite signal.
    cursor = await db.execute(
        "SELECT AVG(confidence) as avg_conf FROM procedural_memory "
        "WHERE deprecated = 0 AND draft = 0",
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
    proposals = await ego_crud.get_proposals_for_quality(
        db, start=since, end=until,
    )
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

    # Overall procedure stats. total counts the whole store (consistent with
    # tier_distribution below); mean_confidence averages VALIDATED procedures
    # only (draft candidates start at ≈0 and would mask real quality).
    cursor = await db.execute(
        """SELECT COUNT(*) as total,
                  AVG(CASE WHEN draft = 0 THEN confidence END) as avg_conf
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


# ── Dimension 7: Approval-Gate Throughput (WS-1 A2) ──────────────────────────

# The self-approval churn class: background sessions requesting CLI fallback
# via the fail-closed gate (autonomy/approval_gate.py). 86% of all rows at
# time of writing — excluded views are reported alongside, never alone.
_CHURN_ACTION_TYPE = "autonomous_cli_fallback"


async def _compute_approvals(
    db: aiosqlite.Connection, since: str, until: str,
) -> tuple[dict, int]:
    """Approval-request resolution metrics over ``approval_requests``.

    Measures GATE THROUGHPUT — how designed approval gates get resolved and
    by whom — NOT corrective intervention: genuinely corrective user action
    (mid-course correction via chat) never lands in this table, so no single
    "intervention rate" headline is derived from it.

    Honesty notes baked into the shape:
    - resolver classes come from the ONE canonical ``classify_resolver``
      (db/crud/approval_requests.py); unmatched values surface as
      ``unknown_resolver_*`` keys so free-text drift is visible in the series.
    - ``system_cancelled`` conflates never-delivered approval cards with
      delivered-but-unanswered ones — there is no delivery-receipt column, so
      no "user ignored rate" is synthesized.
    - ``resolved_at`` carries two formats (space-separated from SQLite
      ``datetime('now')`` bulk expiry; ISO-T+00:00 from Python isoformat) —
      both UTC; ``datetime()`` normalizes them for windowing.

    Snapshot-only dimension (writes no eval_events).
    """
    from genesis.db.crud.approval_requests import classify_resolver

    cursor = await db.execute(
        """SELECT action_type, status, resolved_by
           FROM approval_requests
           WHERE resolved_at IS NOT NULL
             AND datetime(resolved_at) >= datetime(?)
             AND datetime(resolved_at) < datetime(?)""",
        (since, until),
    )
    resolved = [dict(r) for r in await cursor.fetchall()]

    # datetime() here too: production always passes ISO-T created_at, but the
    # CRUD's COALESCE(?, datetime('now')) fallback writes space-format — a
    # future caller omitting created_at must not dodge the window.
    cursor = await db.execute(
        "SELECT COUNT(*) FROM approval_requests "
        "WHERE datetime(created_at) >= datetime(?) "
        "AND datetime(created_at) < datetime(?)",
        (since, until),
    )
    total_created = (await cursor.fetchone())[0]

    # Point-in-time gauge, not windowed: what is open right now.
    cursor = await db.execute(
        "SELECT COUNT(*) FROM approval_requests WHERE status = 'pending'"
    )
    pending_open = (await cursor.fetchone())[0]

    total_resolved = len(resolved)
    classified = [(r, classify_resolver(r["resolved_by"])) for r in resolved]
    user_resolved = sum(1 for _r, c in classified if c == "human")
    auto_resolved = sum(1 for _r, c in classified if c == "system")
    unknown_rows = [r for r, c in classified if c == "unknown"]

    churn_total = sum(
        1 for r in resolved if r["action_type"] == _CHURN_ACTION_TYPE
    )
    non_churn_total = total_resolved - churn_total
    user_non_churn = sum(
        1 for r, c in classified
        if c == "human" and r["action_type"] != _CHURN_ACTION_TYPE
    )

    metrics = {
        "total_created": total_created,
        "total_resolved": total_resolved,
        "churn_total": churn_total,
        "churn_excluded_total": non_churn_total,
        "user_resolved": user_resolved,
        "user_resolved_rate": round(user_resolved / total_resolved, 4)
        if total_resolved else None,
        "user_resolved_rate_excl_churn": round(user_non_churn / non_churn_total, 4)
        if non_churn_total else None,
        "auto_resolved": auto_resolved,
        "auto_expired": sum(1 for r in resolved if r["status"] == "expired"),
        "system_cancelled": sum(
            1 for r, c in classified
            if r["status"] == "cancelled" and c == "system"
        ),
        "rejection_count": sum(1 for r in resolved if r["status"] == "rejected"),
        # M12 scaffold: a human-resolved rejection = an explicit user denial.
        # Zero observed instances to date — derivation is unvalidated against
        # real deny data until a first real rejection occurs.
        "user_denied_count": sum(
            1 for r, c in classified
            if r["status"] == "rejected" and c == "human"
        ),
        "unknown_resolver_count": len(unknown_rows),
        "unknown_resolver_values": sorted(
            {r["resolved_by"] for r in unknown_rows if r["resolved_by"]}
        )[:10],
        "pending_open": pending_open,
    }
    return metrics, total_resolved


# ── Dimension 8: Goal Completion (WS-1 A2, scaffold) ─────────────────────────


async def _compute_goal_completion(
    db: aiosqlite.Connection, since: str, until: str,
) -> tuple[dict, int]:
    """``user_goals`` status ratios + completion rate.

    SCAFFOLD: zero terminal goals exist to date, so ``completion_rate`` is
    None (never 0.0 on 0/0). Rate = achieved / (achieved + abandoned);
    achieved and abandoned are always reported separately — a lone ratio
    would invite abandoning stale goals to juice it. Ex-ante success
    criteria (a ``success_criteria`` column + population wiring) are
    deliberately deferred; until then any non-null rate is unvalidated.

    Snapshot-only dimension (writes no eval_events).
    """
    cursor = await db.execute(
        "SELECT status, COUNT(*) FROM user_goals GROUP BY status"
    )
    by_status = {r[0]: r[1] for r in await cursor.fetchall()}
    total_goals = sum(by_status.values())
    achieved = by_status.get("achieved", 0)
    abandoned = by_status.get("abandoned", 0)
    terminal = achieved + abandoned

    cursor = await db.execute(
        "SELECT COUNT(*) FROM user_goals "
        "WHERE achieved_at >= ? AND achieved_at < ?",
        (since, until),
    )
    achieved_in_period = (await cursor.fetchone())[0]

    # No abandoned_in_period: user_goals has no abandonment timestamp
    # (mark_abandoned only touches updated_at, which ANY later edit also
    # refreshes) — a windowed count would double-report edited-after-abandon
    # goals. The all-time abandoned_count stock (weekly deltas in the series)
    # carries the flow signal honestly.
    metrics = {
        "total_goals": total_goals,
        "by_status": {
            s: by_status.get(s, 0)
            for s in ("active", "paused", "achieved", "abandoned")
        },
        "terminal_count": terminal,
        "achieved_count": achieved,
        "abandoned_count": abandoned,
        "completion_rate": round(achieved / terminal, 4) if terminal else None,
        "achieved_in_period": achieved_in_period,
    }
    if terminal == 0:
        metrics["note"] = "scaffold: no terminal goals yet — rate uncomputable"
    return metrics, total_goals


# ── Dimension 9: Noise / Passivity v1 (WS-1 A2) ──────────────────────────────


async def _compute_noise_passivity(
    db: aiosqlite.Connection, since: str, until: str,
) -> tuple[dict, int]:
    """Noise/passivity v1: loop-closure leaks + windowed ego-cycle activity.

    Two kinds of numbers, deliberately distinct:
    - Funnel gauges (``stale_followups`` …) reuse db/crud/loop_closure.py and
      are ALL-TIME LEVELS (stocks) snapshotted weekly — the *series* shows
      drift, but week-over-week deltas are NOT flows.
    - Windowed counts (``ego_cycles`` …) are true per-period flows.
      ``empty_ego_cycle_pct`` has an opportunity denominator (cycles run in
      the window) — an empty cycle on a quiet week isn't passivity, which is
      why the denominator is cycles, not wall-clock.

    Raw buckets only — no composite "suppression score": realist ``reject``
    verdicts have zero observed rows, so any composite would be dominated by
    unvalidated components.

    Snapshot-only dimension (writes no eval_events).
    """
    from genesis.db.crud import loop_closure

    stale_before = (
        datetime.fromisoformat(until) - timedelta(days=loop_closure.STALE_DAYS)
    ).isoformat()
    fu = await loop_closure.followup_funnel(db, stale_before=stale_before)
    obs = await loop_closure.observation_funnel(db, stale_before=stale_before)
    prop = await loop_closure.proposal_funnel(db, stale_before=stale_before)

    cursor = await db.execute(
        "SELECT COUNT(*), COALESCE(SUM(num_proposals = 0), 0) "
        "FROM ego_cycle_outcomes WHERE created_at >= ? AND created_at < ?",
        (since, until),
    )
    row = await cursor.fetchone()
    ego_cycles, empty_ego_cycles = row[0], row[1]

    cursor = await db.execute(
        "SELECT COUNT(*) FROM ego_proposals "
        "WHERE created_at >= ? AND created_at < ?",
        (since, until),
    )
    proposals_in_period = (await cursor.fetchone())[0]

    # Decision buckets window on resolved_at (set by the reject/table/withdraw
    # CRUD transitions), NOT created_at — a proposal created before the window
    # but decided inside it IS this week's decision; created_at windowing
    # would undercount decisions on boundary-straddling proposals.
    cursor = await db.execute(
        "SELECT status, COUNT(*) FROM ego_proposals "
        "WHERE resolved_at IS NOT NULL "
        "AND resolved_at >= ? AND resolved_at < ? "
        "AND status IN ('rejected', 'withdrawn', 'tabled') GROUP BY status",
        (since, until),
    )
    decision_counts = {r[0]: r[1] for r in await cursor.fetchall()}

    cursor = await db.execute(
        "SELECT action_type, COUNT(*) FROM ego_proposals "
        "WHERE resolved_at IS NOT NULL "
        "AND resolved_at >= ? AND resolved_at < ? AND status = 'rejected' "
        "GROUP BY action_type",
        (since, until),
    )
    rejected_by_action_type = {r[0]: r[1] for r in await cursor.fetchall()}

    cursor = await db.execute(
        "SELECT realist_verdict, COUNT(*) FROM ego_proposals "
        "WHERE created_at >= ? AND created_at < ? "
        "AND realist_verdict IS NOT NULL GROUP BY realist_verdict",
        (since, until),
    )
    verdicts = {r[0]: r[1] for r in await cursor.fetchall()}

    metrics = {
        # Gauges (all-time stocks, see docstring)
        "stale_followups": fu["leak_pending_stale"],
        "followups_pending": fu["by_status"].get("pending", 0),
        "observations_stale_unactuated": obs["leak_stale_unactuated"],
        "proposals_pending_stale": prop["leak_pending_stale"],
        "stale_cutoff_days": loop_closure.STALE_DAYS,
        # Windowed flows
        "ego_cycles": ego_cycles,
        "empty_ego_cycles": empty_ego_cycles,
        "empty_ego_cycle_pct": round(empty_ego_cycles / ego_cycles, 4)
        if ego_cycles else None,
        "proposals_in_period": proposals_in_period,
        # Decisions RESOLVED in window (not created-in-window cohort)
        "rejected_count": decision_counts.get("rejected", 0),
        "withdrawn_count": decision_counts.get("withdrawn", 0),
        "tabled_count": decision_counts.get("tabled", 0),
        "rejected_by_action_type": rejected_by_action_type,
        "realist_amend": verdicts.get("amend", 0),
        "realist_reject": verdicts.get("reject", 0),
        "realist_quality_hold": verdicts.get("quality_hold", 0),
    }
    return metrics, ego_cycles + proposals_in_period


# ── Dimension 10: Dev Quality (component g) ──────────────────────────────────


def _parse_iso_utc(value: str | None) -> datetime | None:
    """Lenient ISO-8601 parse → aware UTC datetime (None on garbage).

    pr_review_findings ``merged_at`` comes from GitHub (``...Z``); the window
    bounds are Python isoformat (``+00:00``) — fromisoformat handles both,
    and naive values are assumed UTC.
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


async def _compute_dev_quality(
    db: aiosqlite.Connection, since: str, until: str,
) -> tuple[dict, int]:
    """Dev-quality metrics: PR-review findings + code-audit backlog + edit failures.

    Three read paths, no writes:
    - observations category='pr_review_findings' (written by
      eval/pr_review_harvest.py, Sundays 06:45 — 45 min before this runs):
      windowed on the ``merged_at`` INSIDE the content JSON, not the row's
      created_at (re-harvest refreshes rows without changing what week the
      PR merged). ``harvest_prs_seen`` counts ALL rows regardless of window
      so thin harvest coverage is visible, not hidden.
    - observations source='recon' category='code_audit', unresolved — an
      all-time backlog gauge (stock, not flow). ``code_audit_by_category``
      stays sparse until the audit taxonomy prompt ships (see ``note``).
    - tool_call_outcomes windowed on timestamp (Edit/Write rows written by
      scripts/edit_failure_sensor.py). datetime() normalization mirrors the
      approvals dimension: both formats seen in the wild must window.

    Honesty rules: every rate carries its raw numerator + denominator, and
    is None on a zero denominator — NEVER 0.0 on 0/0. Ambiguity is
    acknowledged where it exists (fewer findings/PR = better code OR weaker
    review — the dashboard renders this dimension direction-neutral).

    Snapshot-only dimension (writes no eval_events — the eval_events CHECK
    constraint does not include 'dev_quality', and must not: this is weekly
    aggregate telemetry, not an event stream).
    """
    start_dt = _parse_iso_utc(since)
    end_dt = _parse_iso_utc(until)

    # ── PR-review findings (windowed on content merged_at) ──────────────
    cursor = await db.execute(
        "SELECT content FROM observations WHERE category = 'pr_review_findings'",
    )
    rows = await cursor.fetchall()
    harvest_prs_seen = len(rows)

    prs_merged = 0
    review_findings_total = 0
    by_severity = dict.fromkeys(
        ("blocker", "should_fix", "note", "unlabeled"), 0,
    )
    review_count_total = 0
    for r in rows:
        try:
            payload = json.loads(r["content"] or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        merged = _parse_iso_utc(payload.get("merged_at"))
        if merged is None or start_dt is None or end_dt is None:
            continue
        if not (start_dt <= merged < end_dt):
            continue
        prs_merged += 1
        review_count_total += int(payload.get("review_count") or 0)
        for f in payload.get("findings") or []:
            review_findings_total += 1
            sev = f.get("severity")
            # Unknown severity strings fold into the honest bucket, never
            # a guessed one.
            by_severity[sev if sev in by_severity else "unlabeled"] += 1

    # ── Code-audit backlog (all-time stock; mirrors the surplus panel's
    #    source='recon' + unresolved population so the numbers agree) ─────
    cursor = await db.execute(
        "SELECT content FROM observations "
        "WHERE source = 'recon' AND category = 'code_audit' AND resolved = 0",
    )
    audit_rows = await cursor.fetchall()
    audit_by_severity: dict[str, int] = {}
    audit_by_category: dict[str, int] = {}
    for r in audit_rows:
        try:
            parsed = json.loads(r["content"] or "{}")
        except (json.JSONDecodeError, TypeError):
            parsed = {}
        sev = parsed.get("severity") or "unknown"
        audit_by_severity[sev] = audit_by_severity.get(sev, 0) + 1
        cat = parsed.get("category")
        if cat:
            audit_by_category[cat] = audit_by_category.get(cat, 0) + 1

    # ── Edit failures (windowed flow; Edit/Write only — the sensor's
    #    contract — so a future emitter of other tools can't skew this) ───
    cursor = await db.execute(
        "SELECT COUNT(*), COALESCE(SUM(success = 0), 0) FROM tool_call_outcomes "
        "WHERE tool_name IN ('Edit', 'Write') "
        "AND datetime(timestamp) >= datetime(?) AND datetime(timestamp) < datetime(?)",
        (since, until),
    )
    row = await cursor.fetchone()
    edit_calls_total, edit_failures = row[0], row[1]

    metrics = {
        "prs_merged": prs_merged,
        "review_findings_total": review_findings_total,
        "review_findings_by_severity": by_severity,
        "findings_per_pr": round(review_findings_total / prs_merged, 2)
        if prs_merged else None,
        "review_count_total": review_count_total,
        "mean_reviews_per_pr": round(review_count_total / prs_merged, 2)
        if prs_merged else None,
        "code_audit_open_findings": len(audit_rows),
        "code_audit_by_severity": audit_by_severity,
        "code_audit_by_category": audit_by_category,
        "edit_calls_total": edit_calls_total,
        "edit_failures": edit_failures,
        "edit_failure_rate": round(edit_failures / edit_calls_total, 4)
        if edit_calls_total else None,
        # Coverage honesty: all harvested PR rows, windowed or not.
        "harvest_prs_seen": harvest_prs_seen,
        "note": "scaffold: code_audit_by_category sparse until the audit "
                "taxonomy prompt ships",
    }
    return metrics, prs_merged + edit_calls_total


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


def _score_with_zero_fill(
    available: list[tuple[float | None, float]],
    *,
    max_nulls: int = 1,
) -> float | None:
    """Compute weighted score, treating null factors as 0.0.

    Returns None if more than *max_nulls* factors are null — the grade
    should be withheld rather than computed from mostly-absent data.
    """
    null_count = sum(1 for v, _ in available if v is None)
    if null_count > max_nulls:
        return None
    return sum((v if v is not None else 0.0) * w for v, w in available) * 100


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

    if all(v is None for v, _ in available) or total < _MIN_SAMPLES["memory"]:
        return {"grade": None, "score": None, "factors": factors,
                "sample_count": total,
                "reason": f"insufficient data ({total} recalls, need {_MIN_SAMPLES['memory']})"}

    score = _score_with_zero_fill(available, max_nulls=1)
    if score is None:
        return {"grade": None, "score": None, "factors": factors,
                "sample_count": total,
                "reason": "too many factors unavailable"}

    return {"grade": _score_to_grade(score), "score": round(score, 1),
            "factors": factors, "sample_count": total}


async def _grade_ego(
    db: aiosqlite.Connection,
    since: str,
    until: str,
    dimension_results: dict[str, dict],
) -> dict:
    """Grade ego subsystem from dimension 3 metrics.

    Factors: calibration_accuracy (40%), execution_success (40%),
    approval_rate (20%). Calibration and delivery matter most — an ego
    that proposes bold/rejected ideas with accurate self-assessment
    is healthier than one that only proposes safe bets.
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

    available = [(cal_accuracy, 0.4), (exec_success, 0.4), (approval, 0.2)]

    if all(v is None for v, _ in available) or total < _MIN_SAMPLES["ego"]:
        return {"grade": None, "score": None, "factors": factors,
                "sample_count": total,
                "reason": f"insufficient data ({total} proposals, need {_MIN_SAMPLES['ego']})"}

    score = _score_with_zero_fill(available, max_nulls=1)
    if score is None:
        return {"grade": None, "score": None, "factors": factors,
                "sample_count": total,
                "reason": "too many factors unavailable"}

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

    if invocations < _MIN_SAMPLES["procedural"]:
        return {"grade": None, "score": None, "factors": factors,
                "sample_count": invocations,
                "reason": f"insufficient data ({invocations} invocations, need {_MIN_SAMPLES['procedural']})"}

    # Primary factor gate: success_rate is the most important signal.
    # Without it, grading on confidence + activity alone is misleading.
    if success is None:
        return {"grade": None, "score": None, "factors": factors,
                "sample_count": invocations,
                "reason": "primary metric (success_rate) not yet measurable"}

    score = _score_with_zero_fill(available, max_nulls=1)
    if score is None:
        return {"grade": None, "score": None, "factors": factors,
                "sample_count": invocations,
                "reason": "too many factors unavailable"}

    return {"grade": _score_to_grade(score), "score": round(score, 1),
            "factors": factors, "sample_count": invocations}


async def _grade_awareness(
    db: aiosqlite.Connection,
    since: str,
    until: str,
    _dimension_results: dict[str, dict],
) -> dict:
    """Grade awareness subsystem from awareness_ticks data.

    Factors: tick_regularity (60%), signal_completeness (40%).

    tick_regularity measures whether the awareness loop fires on schedule.
    signal_completeness measures whether all expected signal collectors are
    producing output — catches broken collectors whose signals disappear.

    Classification rate and depth balance are tracked as informational
    factors but NOT scored, because a quiet system (94%+ unclassified
    ticks) is healthy behavior, not a failure.
    """
    # Total ticks in period
    cursor = await db.execute(
        "SELECT COUNT(*) as cnt FROM awareness_ticks "
        "WHERE created_at >= ? AND created_at < ?",
        (since, until),
    )
    row = await cursor.fetchone()
    total_ticks = row["cnt"] if row else 0

    # Tick regularity: expect ~2000+/week (5-min ticks, 2016 per week).
    expected_ticks = 2016  # 7 * 24 * 60 / 5
    tick_regularity = min(total_ticks / expected_ticks, 1.0) if total_ticks else 0.0

    # Signal completeness: are all expected collectors producing output?
    # Sample recent ticks and count distinct signal names.
    cursor = await db.execute(
        "SELECT signals_json FROM awareness_ticks "
        "WHERE created_at >= ? AND created_at < ? "
        "ORDER BY created_at DESC LIMIT 10",
        (since, until),
    )
    signal_names: set[str] = set()
    for tick_row in await cursor.fetchall():
        try:
            raw = tick_row["signals_json"] if isinstance(tick_row, dict) else tick_row[0]
            signals = json.loads(raw)
            for s in signals:
                name = s.get("name", "")
                if name:
                    signal_names.add(name)
        except (json.JSONDecodeError, TypeError, KeyError, AttributeError):
            pass
    # 18 expected signals (21 total minus 3 conditional: genesis/cc version, stale items)
    expected_signals = 18
    signal_completeness = min(len(signal_names) / expected_signals, 1.0) if expected_signals else 0.0

    # Informational: classification stats (not scored)
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

    factors = {"tick_regularity": round(tick_regularity, 4),
               "signal_completeness": round(signal_completeness, 4),
               "unique_signals": len(signal_names),
               "total_ticks": total_ticks,
               "classified_count": classified_count,
               "depth_distribution": depth_dist}

    if total_ticks < _MIN_SAMPLES["awareness"]:
        return {"grade": None, "score": None, "factors": factors,
                "sample_count": total_ticks,
                "reason": f"insufficient data ({total_ticks} ticks, need {_MIN_SAMPLES['awareness']})"}

    available = [(tick_regularity, 0.6), (signal_completeness, 0.4)]
    score = _score_with_zero_fill(available, max_nulls=0)
    if score is None:
        return {"grade": None, "score": None, "factors": factors,
                "sample_count": total_ticks,
                "reason": "factors unavailable"}

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

    available = [(volume_score, 0.25), (influence_rate, 0.5), (type_diversity, 0.25)]
    score = _score_with_zero_fill(available, max_nulls=1)
    if score is None:
        return {"grade": None, "score": None, "factors": factors,
                "sample_count": total_obs,
                "reason": "too many factors unavailable"}

    return {"grade": _score_to_grade(score), "score": round(score, 1),
            "factors": factors, "sample_count": total_obs}
