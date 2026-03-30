"""Tests for depth classifier."""

from datetime import UTC, datetime, timedelta

from genesis.awareness.classifier import classify_depth
from genesis.awareness.types import Depth, DepthScore
from genesis.db.crud import awareness_ticks


def _score(depth, final, threshold, triggered):
    return DepthScore(
        depth=depth, raw_score=final, time_multiplier=1.0,
        final_score=final, threshold=threshold, triggered=triggered,
    )


async def test_highest_depth_wins(db):
    """When multiple depths trigger, return the highest (Deep > Light > Micro)."""
    scores = [
        _score(Depth.MICRO, 0.6, 0.5, True),
        _score(Depth.LIGHT, 0.9, 0.8, True),
        _score(Depth.DEEP, 0.6, 0.55, True),
        _score(Depth.STRATEGIC, 0.3, 0.4, False),
    ]
    result = await classify_depth(db, scores)
    assert result.depth == Depth.DEEP


async def test_nothing_triggered(db):
    """When no depth triggers, return None."""
    scores = [
        _score(Depth.MICRO, 0.3, 0.5, False),
        _score(Depth.LIGHT, 0.4, 0.8, False),
        _score(Depth.DEEP, 0.2, 0.55, False),
        _score(Depth.STRATEGIC, 0.1, 0.4, False),
    ]
    result = await classify_depth(db, scores)
    assert result is None


async def test_ceiling_blocks_trigger(db):
    """If a depth is at ceiling, skip to next lower depth."""
    # Insert 2 Micro ticks in the last hour (ceiling = 2/hr)
    now = datetime.now(UTC)
    for i in range(2):
        await awareness_ticks.create(
            db,
            id=f"ceiling-test-{i}",
            source="scheduled",
            signals_json="[]",
            scores_json="[]",
            classified_depth="Micro",
            created_at=(now - timedelta(minutes=i * 5)).isoformat(),
        )

    scores = [
        _score(Depth.MICRO, 0.6, 0.5, True),    # triggered but at ceiling
        _score(Depth.LIGHT, 0.4, 0.8, False),    # not triggered
        _score(Depth.DEEP, 0.2, 0.55, False),
        _score(Depth.STRATEGIC, 0.1, 0.4, False),
    ]
    result = await classify_depth(db, scores)
    assert result is None  # Micro blocked by ceiling, nothing else triggered


async def test_bypass_ceiling_on_critical(db):
    """force_tick bypass_ceiling=True ignores ceiling limits."""
    now = datetime.now(UTC)
    for i in range(2):
        await awareness_ticks.create(
            db,
            id=f"bypass-test-{i}",
            source="scheduled",
            signals_json="[]",
            scores_json="[]",
            classified_depth="Micro",
            created_at=(now - timedelta(minutes=i * 5)).isoformat(),
        )

    scores = [
        _score(Depth.MICRO, 0.6, 0.5, True),
        _score(Depth.LIGHT, 0.4, 0.8, False),
        _score(Depth.DEEP, 0.2, 0.55, False),
        _score(Depth.STRATEGIC, 0.1, 0.4, False),
    ]
    result = await classify_depth(db, scores, bypass_ceiling=True)
    assert result is not None
    assert result.depth == Depth.MICRO
