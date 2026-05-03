"""Urgency scoring for the Awareness Loop.

Implements: urgency_score(depth) = Σ(signal_value × weight × staleness_factor) × time_multiplier(depth)

Staleness decay: signals whose value hasn't changed since the previous tick
get exponentially reduced weight contribution. This prevents permanently-stuck
signals (e.g. critical_failure=1.0 for hours) from triggering reflections
every tick. First occurrence = full weight; each consecutive unchanged tick
halves the contribution, flooring at 10%.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

import aiosqlite

from genesis.awareness.types import Depth, DepthScore, SignalReading
from genesis.db.crud import awareness_ticks, depth_thresholds, signal_weights

logger = logging.getLogger(__name__)

# ─── Staleness decay ────────────────────────────────────────────────────────
_DECAY_BASE = 0.5   # halve contribution each unchanged tick
_DECAY_FLOOR = 0.10  # never decay below 10% (was 25%, stuck signals triggered micro every ~30min; was 5%, too aggressive)
_EPSILON = 0.001     # float comparison tolerance for 0-1 normalized signals

# Module-level state: consecutive-unchanged count per signal.
# Resets on process restart (acceptable — one free full-weight tick).
_signal_unchanged_counts: dict[str, int] = {}


def get_staleness_context() -> dict[str, int]:
    """Return a snapshot of consecutive-unchanged counts per signal.

    Used by prompt builders to annotate persistent signals.
    """
    return dict(_signal_unchanged_counts)


def _update_staleness(current_signals: dict[str, float], prev_signals: dict[str, float]) -> dict[str, float]:
    """Update unchanged counts and return decay factors per signal."""
    factors: dict[str, float] = {}
    for name, value in current_signals.items():
        prev_value = prev_signals.get(name)
        if prev_value is not None and abs(value - prev_value) < _EPSILON:
            _signal_unchanged_counts[name] = _signal_unchanged_counts.get(name, 0) + 1
        else:
            _signal_unchanged_counts[name] = 0
        count = _signal_unchanged_counts[name]
        factors[name] = max(_DECAY_FLOOR, _DECAY_BASE ** count) if count > 0 else 1.0
    return factors


# ─── Time multiplier curves ──────────────────────────────────────────────────
# Each curve is a list of (elapsed_seconds, multiplier) breakpoints.
# Between breakpoints: linear interpolation. Beyond last: clamp to last value.

_TIME_CURVES: dict[Depth, list[tuple[int, float]]] = {
    Depth.MICRO: [
        (0, 0.5),       # softened from 0.3 — counter reset already suppresses
        (1800, 1.0),    # 30min — floor
        (3600, 2.5),    # 60min — overdue
    ],
    Depth.LIGHT: [
        (0, 0.8),       # softened from 0.5 — counter reset already suppresses
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

    # Fetch previous tick's signals for staleness comparison
    prev_tick = await awareness_ticks.last_tick(db)
    prev_signals: dict[str, float] = {}
    if prev_tick and prev_tick.get("signals_json"):
        try:
            for entry in json.loads(prev_tick["signals_json"]):
                prev_signals[entry["name"]] = entry["value"]
        except (json.JSONDecodeError, KeyError, TypeError):
            logger.debug("Could not parse previous tick signals for staleness")

    decay_factors = _update_staleness(signal_map, prev_signals)

    thresholds = {r["depth_name"]: r for r in await depth_thresholds.list_all(db)}
    results = []

    for depth in Depth:
        # Get signals + weights that feed this depth
        weights_rows = await signal_weights.list_by_depth(db, depth.value)
        raw_score = 0.0
        for w in weights_rows:
            val = signal_map.get(w["signal_name"], 0.0)
            factor = decay_factors.get(w["signal_name"], 1.0)
            raw_score += val * w["current_weight"] * factor

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
