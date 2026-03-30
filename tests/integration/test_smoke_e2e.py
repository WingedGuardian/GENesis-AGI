"""Smoke tests requiring real services or long runtime.

Marked @pytest.mark.slow — skipped in normal CI runs.
Run with: pytest -v -m slow tests/integration/test_smoke_e2e.py
"""

from datetime import UTC, datetime, timedelta

import pytest

pytestmark = [pytest.mark.asyncio, pytest.mark.slow]


async def test_ceiling_resets_after_window(db):
    """Verify ceiling expires correctly with real timestamps.

    Creates a tick from 2h ago, confirms it's outside the 1h ceiling window.
    Then creates a recent tick and confirms it IS in the window.
    """
    from genesis.db.crud import awareness_ticks

    # Create an old tick
    old_ts = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    await awareness_ticks.create(
        db,
        id=f"smoke-ceiling-{datetime.now(UTC).timestamp()}",
        source="scheduled",
        signals_json="[]",
        scores_json="[]",
        classified_depth="Micro",
        trigger_reason="smoke test",
        created_at=old_ts,
    )

    count = await awareness_ticks.count_in_window(
        db, depth="Micro", window_seconds=3600,
    )
    assert count == 0, "Old tick should not appear in 1h ceiling window"

    # Create a recent tick
    recent_ts = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    await awareness_ticks.create(
        db,
        id=f"smoke-ceiling-recent-{datetime.now(UTC).timestamp()}",
        source="scheduled",
        signals_json="[]",
        scores_json="[]",
        classified_depth="Micro",
        trigger_reason="smoke test recent",
        created_at=recent_ts,
    )

    count = await awareness_ticks.count_in_window(
        db, depth="Micro", window_seconds=3600,
    )
    assert count == 1, "Recent tick should appear in 1h ceiling window"


async def test_full_pipeline_observation_to_context(db):
    """Create obs → awareness tick with reflection → context includes obs.

    End-to-end: observation creation → context assembly → observation visible.
    """
    from genesis.db.crud import observations

    # Seed an observation
    await observations.create(
        db, id="pipe-1", source="deep_reflection", type="learning",
        content="discovered important pattern", priority="high",
        created_at=datetime.now(UTC).isoformat(),
    )

    # Build context (simulates what a reflection cycle would see)
    from genesis.reflection.context_gatherer import ContextGatherer

    gatherer = ContextGatherer()
    bundle = await gatherer.gather(db)

    obs_ids = [o["id"] for o in bundle.recent_observations]
    assert "pipe-1" in obs_ids

    # The observation should have had its retrieved_count incremented
    row = await observations.get_by_id(db, "pipe-1")
    assert row["retrieved_count"] >= 1
