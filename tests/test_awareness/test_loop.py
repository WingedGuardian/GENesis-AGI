"""Tests for the AwarenessLoop orchestrator.

Tests the tick pipeline directly (perform_tick) without relying on
APScheduler timing. Scheduler integration is tested separately.
"""

from genesis.awareness.loop import perform_tick
from genesis.awareness.signals import ConversationCollector
from genesis.awareness.types import SignalReading
from genesis.db.crud import awareness_ticks, observations


async def test_perform_tick_no_trigger(db):
    """Tick with zero signals writes to awareness_ticks but not observations."""
    collectors = [ConversationCollector()]
    result = await perform_tick(db, collectors, source="scheduled")

    assert result.classified_depth is None

    # Should be stored in awareness_ticks
    ticks = await awareness_ticks.query(db)
    assert len(ticks) == 1
    assert ticks[0]["source"] == "scheduled"
    assert ticks[0]["classified_depth"] is None

    # Should NOT create an observation
    obs = await observations.query(db, type="awareness_tick")
    assert len(obs) == 0


async def test_perform_tick_with_trigger(db):
    """Tick that triggers a depth writes to both awareness_ticks and observations."""

    class HotSignal:
        signal_name = "software_error_spike"

        async def collect(self):
            return SignalReading(
                name="software_error_spike", value=1.0,
                source="health_mcp", collected_at="2026-03-03T12:00:00+00:00",
            )

    result = await perform_tick(db, [HotSignal()], source="scheduled")

    # software_error_spike (weight 0.70) at value 1.0, max elapsed → high multiplier
    # Should trigger at least Micro (threshold 0.50)
    assert result.classified_depth is not None

    # Check observation was created
    obs = await observations.query(db, type="awareness_tick")
    assert len(obs) == 1
    assert obs[0]["source"] == "awareness_loop"


async def test_perform_tick_critical_bypass(db):
    """Critical bypass ticks are flagged correctly."""
    await perform_tick(
        db, [ConversationCollector()],
        source="critical_bypass", reason="test emergency",
    )
    ticks = await awareness_ticks.query(db, source="critical_bypass")
    assert len(ticks) == 1


async def test_perform_tick_idempotent_ids(db):
    """Each tick gets a unique ID."""
    collectors = [ConversationCollector()]
    r1 = await perform_tick(db, collectors, source="scheduled")
    r2 = await perform_tick(db, collectors, source="scheduled")
    assert r1.tick_id != r2.tick_id


async def test_replace_collectors(db):
    """replace_collectors swaps the collector list on a running loop."""
    from unittest.mock import MagicMock

    from genesis.awareness.loop import AwarenessLoop

    stub = MagicMock()
    stub.signal_name = "stub"
    loop = AwarenessLoop(db=db, collectors=[stub])
    assert len(loop._collectors) == 1
    assert loop._collectors[0].signal_name == "stub"

    new1 = MagicMock()
    new1.signal_name = "real_a"
    new2 = MagicMock()
    new2.signal_name = "real_b"
    loop.replace_collectors([new1, new2])

    assert len(loop._collectors) == 2
    assert loop._collectors[0].signal_name == "real_a"
    assert loop._collectors[1].signal_name == "real_b"
