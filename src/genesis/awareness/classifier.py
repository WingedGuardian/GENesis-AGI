"""Depth classification for the Awareness Loop.

Selects the highest triggered depth that isn't blocked by ceiling or floor constraints.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import aiosqlite

from genesis.awareness.types import Depth, DepthScore
from genesis.db.crud import awareness_ticks, depth_thresholds

# Priority order: highest to lowest
_DEPTH_PRIORITY = [Depth.STRATEGIC, Depth.DEEP, Depth.LIGHT, Depth.MICRO]


@dataclass(frozen=True)
class DepthDecision:
    """Result of depth classification."""

    depth: Depth
    score: DepthScore
    reason: str


async def classify_depth(
    db: aiosqlite.Connection,
    scores: list[DepthScore],
    *,
    bypass_ceiling: bool = False,
) -> DepthDecision | None:
    """Select the highest triggered depth not blocked by ceiling or floor constraints.

    Returns None if nothing triggered or all triggered depths are blocked.
    """
    score_map = {s.depth: s for s in scores}
    thresholds = {r["depth_name"]: r for r in await depth_thresholds.list_all(db)}

    for depth in _DEPTH_PRIORITY:
        ds = score_map.get(depth)
        if ds is None or not ds.triggered:
            continue

        # Check ceiling and floor unless bypassed (critical event)
        if not bypass_ceiling:
            cfg = thresholds[depth.value]

            # Ceiling: max N reflections per window
            recent = await awareness_ticks.count_in_window(
                db,
                depth=depth.value,
                window_seconds=cfg["ceiling_window_seconds"],
            )
            if recent >= cfg["ceiling_count"]:
                continue  # At ceiling — try next lower depth

            # Floor: minimum interval between reflections at this depth
            floor_s = cfg["floor_seconds"]
            if floor_s > 0:
                last = await awareness_ticks.last_at_depth(db, depth.value)
                if last is not None:
                    last_dt = datetime.fromisoformat(last["created_at"])
                    elapsed = (datetime.now(UTC) - last_dt).total_seconds()
                    if elapsed < floor_s:
                        continue  # Too soon — try next lower depth

        reason = f"{depth.value} triggered: score {ds.final_score:.3f} >= {ds.threshold:.3f}"
        if bypass_ceiling:
            reason = f"CRITICAL BYPASS — {reason}"
        return DepthDecision(depth=depth, score=ds, reason=reason)

    return None
