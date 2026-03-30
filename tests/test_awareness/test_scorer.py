"""Tests for urgency scorer."""

from genesis.awareness.scorer import compute_scores, compute_time_multiplier
from genesis.awareness.types import Depth, SignalReading

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
