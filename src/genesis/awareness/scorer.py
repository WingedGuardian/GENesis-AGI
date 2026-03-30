"""Urgency scoring for the Awareness Loop.

Implements: urgency_score(depth) = Σ(signal_value × weight) × time_multiplier(depth)
"""

from __future__ import annotations

from datetime import UTC, datetime

import aiosqlite

from genesis.awareness.types import Depth, DepthScore, SignalReading
from genesis.db.crud import awareness_ticks, depth_thresholds, signal_weights

# ─── Time multiplier curves ──────────────────────────────────────────────────
# Each curve is a list of (elapsed_seconds, multiplier) breakpoints.
# Between breakpoints: linear interpolation. Beyond last: clamp to last value.

_TIME_CURVES: dict[Depth, list[tuple[int, float]]] = {
    Depth.MICRO: [
        (0, 0.3),       # just happened
        (1800, 1.0),    # 30min — floor
        (3600, 2.5),    # 60min — overdue
    ],
    Depth.LIGHT: [
        (0, 0.5),
        (10800, 1.0),   # 3h
        (21600, 1.5),   # 6h — floor
        (43200, 3.0),   # 12h — alarm
    ],
    Depth.DEEP: [
        (0, 0.3),
        (172800, 1.0),  # 48h — floor
        (259200, 1.5),  # 72h
        (345600, 2.5),  # 96h — overdue
    ],
    Depth.STRATEGIC: [
        (0, 0.2),
        (432000, 1.0),   # 5d — floor
        (864000, 2.0),   # 10d
        (1296000, 3.0),  # 15d — overdue
    ],
}


def compute_time_multiplier(depth: Depth, *, elapsed_seconds: int) -> float:
    """Piecewise linear interpolation on the time-multiplier curve."""
    curve = _TIME_CURVES[depth]
    if elapsed_seconds <= curve[0][0]:
        return curve[0][1]
    if elapsed_seconds >= curve[-1][0]:
        return curve[-1][1]
    for i in range(len(curve) - 1):
        t0, m0 = curve[i]
        t1, m1 = curve[i + 1]
        if t0 <= elapsed_seconds <= t1:
            ratio = (elapsed_seconds - t0) / (t1 - t0)
            return round(m0 + ratio * (m1 - m0), 4)
    return curve[-1][1]  # fallback (unreachable)


async def compute_scores(
    db: aiosqlite.Connection,
    signals: list[SignalReading],
    *,
    now: str | None = None,
) -> list[DepthScore]:
    """Compute urgency scores for all four depths."""
    if now is None:
        now = datetime.now(UTC).isoformat()

    signal_map = {s.name: s.value for s in signals}
    thresholds = {r["depth_name"]: r for r in await depth_thresholds.list_all(db)}
    results = []

    for depth in Depth:
        # Get signals + weights that feed this depth
        weights_rows = await signal_weights.list_by_depth(db, depth.value)
        raw_score = 0.0
        for w in weights_rows:
            val = signal_map.get(w["signal_name"], 0.0)
            raw_score += val * w["current_weight"]

        # Elapsed time since last tick at this depth
        last_tick = await awareness_ticks.last_at_depth(db, depth.value)
        if last_tick is not None:
            last_dt = datetime.fromisoformat(last_tick["created_at"])
            now_dt = datetime.fromisoformat(now)
            elapsed = int((now_dt - last_dt).total_seconds())
        else:
            # No prior tick — treat as maximally overdue
            elapsed = _TIME_CURVES[depth][-1][0]

        multiplier = compute_time_multiplier(depth, elapsed_seconds=elapsed)
        final = round(raw_score * multiplier, 4)

        threshold_val = thresholds[depth.value]["threshold"]
        results.append(DepthScore(
            depth=depth,
            raw_score=round(raw_score, 4),
            time_multiplier=multiplier,
            final_score=final,
            threshold=threshold_val,
            triggered=final >= threshold_val,
        ))

    return results
