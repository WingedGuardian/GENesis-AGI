"""Tests for urgency scorer."""

import json

from genesis.awareness.scorer import (
    _signal_unchanged_counts,
    _update_staleness,
    compute_scores,
    compute_time_multiplier,
    get_staleness_context,
)
from genesis.awareness.types import Depth, SignalReading
from genesis.db.crud import awareness_ticks

# ─── Time multiplier curve tests ─────────────────────────────────────────────


def test_micro_multiplier_at_zero():
    """Micro: 0.3x at 0 minutes elapsed."""
    assert compute_time_multiplier(Depth.MICRO, elapsed_seconds=0) == 0.3


def test_micro_multiplier_at_floor():
    """Micro: 1.0x at 30 minutes (1800s)."""
    assert compute_time_multiplier(Depth.MICRO, elapsed_seconds=1800) == 1.0


def test_micro_multiplier_at_overdue():
    """Micro: 2.5x at 60 minutes (3600s)."""
    assert compute_time_multiplier(Depth.MICRO, elapsed_seconds=3600) == 2.5


def test_micro_multiplier_interpolated():
    """Micro: halfway between 0min (0.3) and 30min (1.0) should be ~0.65."""
    result = compute_time_multiplier(Depth.MICRO, elapsed_seconds=900)
    assert abs(result - 0.65) < 0.01


def test_light_multiplier_at_zero():
    assert compute_time_multiplier(Depth.LIGHT, elapsed_seconds=0) == 0.5


def test_light_multiplier_at_3h():
    """Light: 1.0x at 3 hours."""
    assert compute_time_multiplier(Depth.LIGHT, elapsed_seconds=10800) == 1.0


def test_light_multiplier_at_6h():
    """Light: 1.5x at 6 hours (floor)."""
    assert compute_time_multiplier(Depth.LIGHT, elapsed_seconds=21600) == 1.5


def test_light_multiplier_at_12h():
    """Light: 3.0x at 12 hours (alarm)."""
    assert compute_time_multiplier(Depth.LIGHT, elapsed_seconds=43200) == 3.0


def test_deep_multiplier_at_zero():
    assert compute_time_multiplier(Depth.DEEP, elapsed_seconds=0) == 0.3


def test_deep_multiplier_at_48h():
    """Deep: 1.0x at 48 hours (floor)."""
    assert compute_time_multiplier(Depth.DEEP, elapsed_seconds=172800) == 1.0


def test_deep_multiplier_at_72h():
    """Deep: 1.5x at 72 hours."""
    assert compute_time_multiplier(Depth.DEEP, elapsed_seconds=259200) == 1.5


def test_deep_multiplier_at_96h():
    """Deep: 2.5x at 96 hours (overdue)."""
    assert compute_time_multiplier(Depth.DEEP, elapsed_seconds=345600) == 2.5


def test_strategic_multiplier_at_zero():
    assert compute_time_multiplier(Depth.STRATEGIC, elapsed_seconds=0) == 0.2


def test_strategic_multiplier_at_5d():
    """Strategic: 1.0x at 5 days (floor)."""
    assert compute_time_multiplier(Depth.STRATEGIC, elapsed_seconds=432000) == 1.0


def test_strategic_multiplier_at_10d():
    """Strategic: 2.0x at 10 days."""
    assert compute_time_multiplier(Depth.STRATEGIC, elapsed_seconds=864000) == 2.0


def test_strategic_multiplier_at_15d():
    """Strategic: 3.0x at 15 days."""
    assert compute_time_multiplier(Depth.STRATEGIC, elapsed_seconds=1296000) == 3.0


def test_multiplier_caps_at_max():
    """Beyond the last defined point, multiplier should not exceed the max."""
    result = compute_time_multiplier(Depth.MICRO, elapsed_seconds=99999)
    assert result == 2.5


# ─── Score computation tests ─────────────────────────────────────────────────


async def test_compute_scores_basic(db):
    """Known signals and weights produce expected scores."""
    signals = [
        SignalReading(
            name="software_error_spike", value=1.0,
            source="health_mcp", collected_at="2026-03-03T12:00:00+00:00",
        ),
    ]
    # software_error_spike feeds ["Micro", "Light"] with weight 0.70
    # With no prior ticks, elapsed time is large → high multiplier
    scores = await compute_scores(db, signals, now="2026-03-03T12:00:00+00:00")
    micro_score = next(s for s in scores if s.depth == Depth.MICRO)
    # raw = 1.0 * 0.70 = 0.70, multiplier at max elapsed = 2.5
    assert micro_score.raw_score == 0.70


async def test_compute_scores_returns_all_depths(db):
    """Should return a score for each depth."""
    scores = await compute_scores(db, [], now="2026-03-03T12:00:00+00:00")
    depths = {s.depth for s in scores}
    assert depths == {Depth.MICRO, Depth.LIGHT, Depth.DEEP, Depth.STRATEGIC}


async def test_compute_scores_zero_signals(db):
    """With no signal readings, raw scores should be 0."""
    scores = await compute_scores(db, [], now="2026-03-03T12:00:00+00:00")
    for s in scores:
        assert s.raw_score == 0.0


# ─── Staleness decay tests ──────────────────────────────────────────────────


def test_update_staleness_unchanged_signal():
    """Unchanged signal gets decaying factor."""
    _signal_unchanged_counts.clear()
    current = {"sig_a": 1.0}
    prev = {"sig_a": 1.0}
    factors = _update_staleness(current, prev)
    assert factors["sig_a"] == 0.5  # first unchanged tick: 0.5
    assert _signal_unchanged_counts["sig_a"] == 1

    # Second unchanged tick
    factors = _update_staleness(current, prev)
    assert factors["sig_a"] == 0.25  # 0.5^2
    assert _signal_unchanged_counts["sig_a"] == 2


def test_update_staleness_changed_signal():
    """Changed signal resets to full weight."""
    _signal_unchanged_counts.clear()
    _signal_unchanged_counts["sig_a"] = 5  # previously stale

    current = {"sig_a": 0.8}
    prev = {"sig_a": 0.3}  # different value
    factors = _update_staleness(current, prev)
    assert factors["sig_a"] == 1.0  # reset to full weight
    assert _signal_unchanged_counts["sig_a"] == 0


def test_update_staleness_new_signal():
    """Signal not in previous tick gets full weight."""
    _signal_unchanged_counts.clear()
    current = {"sig_a": 1.0}
    prev = {}  # signal didn't exist before
    factors = _update_staleness(current, prev)
    assert factors["sig_a"] == 1.0
    assert _signal_unchanged_counts["sig_a"] == 0


def test_update_staleness_decay_floor():
    """Decay should not go below the floor (0.05)."""
    _signal_unchanged_counts.clear()
    _signal_unchanged_counts["sig_a"] = 10  # very stale
    current = {"sig_a": 1.0}
    prev = {"sig_a": 1.0}
    factors = _update_staleness(current, prev)
    assert factors["sig_a"] == 0.05  # floored at 5%


def test_get_staleness_context_returns_copy():
    """get_staleness_context should return a copy, not the mutable dict."""
    _signal_unchanged_counts.clear()
    _signal_unchanged_counts["test"] = 3
    ctx = get_staleness_context()
    assert ctx == {"test": 3}
    ctx["test"] = 99
    assert _signal_unchanged_counts["test"] == 3  # original unchanged


async def test_compute_scores_staleness_decay(db):
    """Identical signals across consecutive ticks produce decayed raw scores."""
    _signal_unchanged_counts.clear()
    signals = [
        SignalReading(
            name="software_error_spike", value=1.0,
            source="health_mcp", collected_at="2026-03-03T12:00:00+00:00",
        ),
    ]

    # First call — no prior tick in DB, signal treated as fresh
    scores1 = await compute_scores(db, signals, now="2026-03-03T12:00:00+00:00")
    micro1 = next(s for s in scores1 if s.depth == Depth.MICRO)

    # Store a tick so the next call has a prior to compare against
    await awareness_ticks.create(
        db,
        id="tick-1",
        source="scheduled",
        signals_json=json.dumps([{"name": "software_error_spike", "value": 1.0}]),
        scores_json="[]",
        created_at="2026-03-03T12:00:00+00:00",
    )

    # Second call — same signal value, should see decay
    scores2 = await compute_scores(db, signals, now="2026-03-03T12:05:00+00:00")
    micro2 = next(s for s in scores2 if s.depth == Depth.MICRO)

    assert micro2.raw_score < micro1.raw_score, (
        f"Stale signal should produce lower raw score: {micro2.raw_score} >= {micro1.raw_score}"
    )
