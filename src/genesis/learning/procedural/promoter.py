"""Procedure promotion and demotion pipeline.

Evaluates all active procedures for tier changes based on confidence and
success/failure history. Runs as a scheduled background job (hourly).

Promotion thresholds:
  L4 → L3: success_count >= 3 AND confidence >= 0.65 AND speculative = 0
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

import aiosqlite

from genesis.db.crud import procedural
from genesis.learning.procedural.trigger_cache import regenerate

logger = logging.getLogger(__name__)

_TIER_RANK = {"L1": 4, "L2": 3, "L3": 2, "L4": 1}
_RANK_TIER = {4: "L1", 3: "L2", 2: "L3", 1: "L4"}


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
    spec = row.get("speculative", 1)
    has_trigger = bool(row.get("tool_trigger"))
    current = row.get("activation_tier") or "L4"

    qualified = "L4"
    if s >= 8 and conf >= 0.85 and has_trigger:
        qualified = "L1"
    elif s >= 5 and conf >= 0.75:
        qualified = "L2"
    elif s >= 3 and conf >= 0.65 and spec == 0:
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

    for row in rows:
        current_tier = row.get("activation_tier", "L4")
        proc_id = row["id"]

        # Quarantine check
        if row["confidence"] < 0.3 and row["success_count"] + row["failure_count"] >= 3:
            await procedural.quarantine(db, proc_id)
            quarantined += 1
            logger.info("Quarantined procedure %s (conf=%.2f)", row["task_type"], row["confidence"])
            continue

        # Compute target tier
        target_tier = _compute_tier(row)

        # Check for demotion
        if _check_demotion(row):
            current_rank = _TIER_RANK.get(current_tier, 1)
            if current_rank > 1:
                target_tier = _RANK_TIER[current_rank - 1]

        if target_tier != current_tier:
            await procedural.update(db, proc_id, activation_tier=target_tier)
            target_rank = _TIER_RANK.get(target_tier, 1)
            current_rank = _TIER_RANK.get(current_tier, 1)
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

    result = {"promotions": promotions, "demotions": demotions, "quarantined": quarantined}
    if any(v > 0 for v in result.values()):
        logger.info("Promotion results: %s", result)
    return result
