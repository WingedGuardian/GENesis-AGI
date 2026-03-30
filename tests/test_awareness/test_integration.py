"""End-to-end integration test for the Awareness Loop.

Verifies the full pipeline: signals → score → classify → store,
with real DB seed data and realistic signal values.
"""

import json
from datetime import UTC, datetime

from genesis.awareness.loop import perform_tick
from genesis.awareness.signals import (
    BudgetCollector,
    ConversationCollector,
    CriticalFailureCollector,
    ErrorSpikeCollector,
    MemoryBacklogCollector,
    OutreachEngagementCollector,
    ReconFindingsCollector,
    StrategicTimerCollector,
    TaskQualityCollector,
)
from genesis.awareness.types import Depth, SignalReading
from genesis.db.crud import awareness_ticks, observations


async def test_full_pipeline_quiet(db):
    """All stub collectors (0.0 values) → nothing triggers."""
    collectors = [
        ConversationCollector(), TaskQualityCollector(),
        OutreachEngagementCollector(), ReconFindingsCollector(),
        MemoryBacklogCollector(), BudgetCollector(),
        ErrorSpikeCollector(), CriticalFailureCollector(),
        StrategicTimerCollector(),
    ]
    result = await perform_tick(db, collectors, source="scheduled")

    # All signals are 0.0, but time multipliers are maxed (no prior ticks)
    # 0.0 * any_weight = 0.0, 0.0 * any_multiplier = 0.0 → nothing triggers
    assert result.classified_depth is None
    assert len(result.signals) == 9
    assert len(result.scores) == 4

    # Tick stored
    ticks = await awareness_ticks.query(db)
    assert len(ticks) == 1

    # No observation
    obs = await observations.query(db, type="awareness_tick")
    assert len(obs) == 0


async def test_full_pipeline_error_spike(db):
    """A saturated error spike signal should trigger Micro or Light."""

    class HotError:
        signal_name = "software_error_spike"

        async def collect(self):
            return SignalReading(
                name="software_error_spike", value=1.0,
                source="health_mcp", collected_at=datetime.now(UTC).isoformat(),
            )

    collectors = [HotError(), ConversationCollector()]
    result = await perform_tick(db, collectors, source="scheduled")

    # error_spike feeds Micro (weight 0.70) and Light (weight 0.70)
    # value=1.0, weight=0.70, max elapsed multiplier → well above thresholds
    assert result.classified_depth is not None
    assert result.classified_depth in (Depth.MICRO, Depth.LIGHT)

    # Observation created for the trigger
    obs = await observations.query(db, type="awareness_tick")
    assert len(obs) == 1
    content = json.loads(obs[0]["content"])
    assert "tick_id" in content


async def test_full_pipeline_critical_bypass(db):
    """force_tick path works end-to-end."""

    class CritSignal:
        signal_name = "critical_failure"

        async def collect(self):
            return SignalReading(
                name="critical_failure", value=1.0,
                source="health_mcp", collected_at=datetime.now(UTC).isoformat(),
            )

    await perform_tick(
        db, [CritSignal()],
        source="critical_bypass", reason="cascading failure",
    )

    ticks = await awareness_ticks.query(db, source="critical_bypass")
    assert len(ticks) == 1


async def test_sequential_ticks_track_history(db):
    """Multiple ticks create a history that affects time multipliers."""
    collectors = [ConversationCollector()]

    await perform_tick(db, collectors, source="scheduled")
    await perform_tick(db, collectors, source="scheduled")

    ticks = await awareness_ticks.query(db)
    assert len(ticks) == 2
    assert ticks[0]["id"] != ticks[1]["id"]
