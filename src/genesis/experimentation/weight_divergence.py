"""Target-2: deterministic awareness signal-weight divergence report (Phase 7).

Measures how a signal-weight change would shift awareness depth decisions,
replayed over historical ticks — WITHOUT mutating the live ``signal_weights``
table or touching live cognition (uses ``compute_scores(weights_override=...)``).

This is an IMPACT / DIVERGENCE report, NOT a win-rate: there is no ground-truth
"correct depth" for an awareness tick, so a win-rate would be meaningless (per
the firsthand DD finding). Instead it quantifies behavioural impact ("this weight
change flips N% of historical ticks' depth decisions, skewing toward more-Deep /
fewer-Micro") for a human promote/no-go decision — recommend-only.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from typing import TYPE_CHECKING

from genesis.awareness.scorer import compute_scores
from genesis.awareness.types import SignalReading
from genesis.db.crud import awareness_ticks

if TYPE_CHECKING:
    import aiosqlite

logger = logging.getLogger(__name__)


def _triggered_depths(scores) -> set[str]:
    return {s.depth.value for s in scores if s.triggered}


def _signals_from_tick(raw: str) -> list[SignalReading] | None:
    try:
        entries = json.loads(raw)
        return [
            SignalReading(
                name=e["name"],
                value=float(e["value"]),
                source=e.get("source", "replay"),
                collected_at=e.get("collected_at", ""),
            )
            for e in entries
        ]
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


async def weight_divergence(
    db: aiosqlite.Connection,
    *,
    signal_weight_overrides: dict[str, float],
    limit: int = 200,
) -> dict:
    """Replay historical ticks under baseline vs overridden weights; report flips.

    Returns:
        dict with n_ticks, n_flipped, flip_rate, depths_gained (depth -> #ticks
        where it NEWLY triggers under the variant), depths_lost, n_skipped, overrides.
    """
    if not signal_weight_overrides:
        raise ValueError("signal_weight_overrides must be non-empty")

    ticks = await awareness_ticks.query(db, limit=limit)
    n_ticks = 0
    n_flipped = 0
    skipped = 0
    gained: Counter = Counter()
    lost: Counter = Counter()

    for tick in ticks:
        raw = tick.get("signals_json")
        if not raw:
            skipped += 1
            continue
        signals = _signals_from_tick(raw)
        if signals is None:
            skipped += 1
            continue

        # decay_factors={}: no staleness decay AND — critically — does NOT call
        # _update_staleness, so replaying historical ticks never mutates the live
        # awareness loop's module-level staleness counters. Both arms share the
        # same (no-decay) basis, so the only difference is the weight override.
        base = await compute_scores(db, signals, decay_factors={})
        var = await compute_scores(
            db, signals, weights_override=signal_weight_overrides, decay_factors={},
        )
        base_t = _triggered_depths(base)
        var_t = _triggered_depths(var)

        n_ticks += 1
        if base_t != var_t:
            n_flipped += 1
            for d in var_t - base_t:
                gained[d] += 1
            for d in base_t - var_t:
                lost[d] += 1

    flip_rate = (n_flipped / n_ticks) if n_ticks else 0.0
    return {
        "n_ticks": n_ticks,
        "n_flipped": n_flipped,
        "flip_rate": round(flip_rate, 4),
        "depths_gained": dict(gained),
        "depths_lost": dict(lost),
        "n_skipped": skipped,
        "overrides": signal_weight_overrides,
    }
