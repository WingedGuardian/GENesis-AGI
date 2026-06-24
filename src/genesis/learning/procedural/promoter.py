"""Procedure promotion and demotion pipeline.

Evaluates all active procedures for tier changes based on confidence and
success/failure history. Runs as a scheduled background job (hourly).

Promotion thresholds:
  L4 → L3: success_count >= 3 AND confidence >= 0.65
  L3 → L2: success_count >= 5 AND confidence >= 0.75
  L2 → L1: success_count >= 8 AND confidence >= 0.85 AND tool_trigger set

Demotion is **evidence-driven only** — never metrics-based:
  3+ failure-mode hits AND failure_count >= success_count + 3 → tier - 1
  confidence < 0.3 AND total samples >= 3                       → quarantine

`_compute_tier` only PROMOTES. If a procedure's metrics no longer support
its current tier (e.g., a seeded L3 with success_count=1, or a procedure
that was created at a higher tier than its raw counts justify), it stays
where it is until either (a) it earns enough successes to be promoted
further, or (b) it accumulates real failures and trips `_check_demotion`
or the quarantine guard. This prevents seeded and explicitly-taught
procedures from being silently downgraded between hourly runs.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

import aiosqlite

from genesis.db.crud import procedural
from genesis.learning.procedural.operations import (
    READ_CONFIDENCE_DISCOUNT,
    effective_confidence,
)
from genesis.learning.procedural.trigger_cache import regenerate

logger = logging.getLogger(__name__)

_TIER_RANK = {"L1": 4, "L2": 3, "L3": 2, "L4": 1}
_RANK_TIER = {4: "L1", 3: "L2", 2: "L3", 1: "L4"}


def _read_eligible_tier(eff_success: int, eff_conf: float, real_success: int) -> str:
    """Tier a procedure qualifies for from *effective* (read-inclusive) metrics.

    Hybrid guard (reads are a dampened signal, not proof of success):
      - L3 (passive surfacing): reads alone may qualify.
      - L2 (advisory-eligible): requires >= 1 *real* success.
      - L1 (always-on): never reachable from reads.

    Mirrors `_compute_tier`'s real-metric thresholds, minus the L1 branch.
    """
    if eff_success >= 5 and eff_conf >= 0.75 and real_success >= 1:
        return "L2"
    if eff_success >= 3 and eff_conf >= 0.65:
        return "L3"
    return "L4"


def _compute_tier(row: dict) -> str:
    """Compute the target tier for a procedure based on its metrics.

    Strict promote-only. Returns the highest tier the row's metrics
    qualify for, then compares against the row's CURRENT tier and never
    returns a lower rank — even if a lower-tier rule still matches. This
    prevents one-failure metric drift (e.g., an L1 procedure whose conf
    drifts from 0.86 → 0.83) from silently downgrading the tier.

    Evidence-driven demotions are handled separately by `_check_demotion`
    (failure history) and the quarantine guard in `promote_and_demote`
    (confidence < 0.3 with sample floor).
    """
    s = row["success_count"]
    conf = row["confidence"]
    has_trigger = bool(row.get("tool_trigger"))
    current = row.get("activation_tier") or "L4"

    qualified = "L4"
    if s >= 8 and conf >= 0.85 and has_trigger:
        qualified = "L1"
    elif s >= 5 and conf >= 0.75:
        qualified = "L2"
    elif s >= 3 and conf >= 0.65:
        qualified = "L3"

    # Promote-only: never demote via metrics drift.
    if _TIER_RANK.get(qualified, 1) > _TIER_RANK.get(current, 1):
        return qualified
    return current


def _check_demotion(row: dict) -> bool:
    """Check if procedure should be demoted based on recent failures."""
    modes = json.loads(row["failure_modes"]) if row.get("failure_modes") else []
    # Count total recent hits across all failure modes
    total_hits = sum(m.get("times_hit", 0) for m in modes)
    # Demote if failure_count exceeds success_count by 3+ (consecutive failure proxy)
    return row["failure_count"] >= row["success_count"] + 3 and total_hits >= 3


async def promote_and_demote(db: aiosqlite.Connection) -> dict:
    """Evaluate all active procedures for tier promotion/demotion.

    Returns summary: {"promotions": N, "demotions": N, "quarantined": N}.
    """
    rows = await procedural.list_active(db)
    promotions = 0
    demotions = 0
    quarantined = 0
    despeculated = 0

    for row in rows:
        current_tier = row.get("activation_tier", "L4")
        proc_id = row["id"]

        # Quarantine check — uses REAL stored confidence + real counts (reads
        # never mask a genuinely-failing procedure).
        if row["confidence"] < 0.3 and row["success_count"] + row["failure_count"] >= 3:
            history = json.loads(row.get("promotion_history") or "[]")
            history.append({
                "from_tier": current_tier,
                "to_tier": "quarantined",
                "at": datetime.now(UTC).isoformat(),
                "reason": "low_confidence_quarantine",
            })
            await procedural.update(db, proc_id, promotion_history=json.dumps(history))
            await procedural.quarantine(db, proc_id)
            quarantined += 1
            logger.info("Quarantined procedure %s (conf=%.2f)", row["task_type"], row["confidence"])
            continue

        # De-speculation: a draft graduates to validated once it has >=1 real
        # success and no failures (reads alone do NOT de-speculate). Closes the
        # gap where nothing ever cleared speculative=1.
        if row["speculative"] and row["success_count"] >= 1 and row["failure_count"] == 0:
            await procedural.update(db, proc_id, speculative=0)
            despeculated += 1
            logger.info("De-speculated procedure %s (success=%d)", row["task_type"], row["success_count"])

        # Compute target tier. Failure evidence (`_check_demotion`) takes
        # precedence over BOTH real-metric and read-driven promotion: a
        # failing procedure must never be promoted — least of all by soft read
        # signal. Otherwise the target is the higher of real-metric and
        # read-driven (effective-metric) promotion (reads can only *raise* it).
        real_target = _compute_tier(row)
        if _check_demotion(row):
            target_tier = real_target  # promote-only on real metrics; held for failing rows
            current_rank = _TIER_RANK.get(current_tier, 1)
            if current_rank > 1:
                target_tier = _RANK_TIER[current_rank - 1]
        else:
            reads = row.get("invocation_count") or 0
            eff_success = row["success_count"] + reads // READ_CONFIDENCE_DISCOUNT
            eff_conf = effective_confidence(
                row["success_count"], row["failure_count"], reads
            )
            read_target = _read_eligible_tier(eff_success, eff_conf, row["success_count"])
            target_tier = (
                real_target
                if _TIER_RANK[real_target] >= _TIER_RANK[read_target]
                else read_target
            )

        if target_tier != current_tier:
            target_rank = _TIER_RANK.get(target_tier, 1)
            current_rank = _TIER_RANK.get(current_tier, 1)
            reason = "metrics_promotion" if target_rank > current_rank else "failure_demotion"

            history = json.loads(row.get("promotion_history") or "[]")
            history.append({
                "from_tier": current_tier,
                "to_tier": target_tier,
                "at": datetime.now(UTC).isoformat(),
                "reason": reason,
            })
            await procedural.update(
                db, proc_id,
                activation_tier=target_tier,
                promotion_history=json.dumps(history),
            )

            if target_rank > current_rank:
                promotions += 1
                logger.info("Promoted %s: %s → %s", row["task_type"], current_tier, target_tier)
            else:
                demotions += 1
                logger.info("Demoted %s: %s → %s", row["task_type"], current_tier, target_tier)

    # Regenerate L1 trigger cache if any tier changes occurred
    if promotions or demotions or quarantined:
        try:
            await regenerate(db)
        except Exception:
            logger.error("Failed to regenerate trigger cache after promotion", exc_info=True)

    result = {
        "promotions": promotions,
        "demotions": demotions,
        "quarantined": quarantined,
        "despeculated": despeculated,
    }
    if any(v > 0 for v in result.values()):
        logger.info("Promotion results: %s", result)
    return result
