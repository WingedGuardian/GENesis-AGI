"""Tests for signal collectors."""

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
    collect_all,
)
from genesis.awareness.types import SignalReading


async def test_stub_collector_returns_zero():
    """Phase 1 stub collectors return 0.0."""
    c = ConversationCollector()
    reading = await c.collect()
    assert isinstance(reading, SignalReading)
    assert reading.value == 0.0
    assert reading.name == "conversations_since_reflection"


async def test_all_collectors_have_correct_signal_names():
    """Each collector's signal_name matches the DB seed data."""
    expected = {
        "conversations_since_reflection",
        "task_completion_quality",
        "outreach_engagement_data",
        "recon_findings_pending",
        "unprocessed_memory_backlog",
        "budget_pct_consumed",
        "software_error_spike",
        "critical_failure",
        "time_since_last_strategic",
    }
    collectors = [
        ConversationCollector(),
        TaskQualityCollector(),
        OutreachEngagementCollector(),
        ReconFindingsCollector(),
        MemoryBacklogCollector(),
        BudgetCollector(),
        ErrorSpikeCollector(),
        CriticalFailureCollector(),
        StrategicTimerCollector(),
    ]
    names = {c.signal_name for c in collectors}
    assert names == expected


async def test_collect_all_returns_nine_readings():
    collectors = [
        ConversationCollector(),
        TaskQualityCollector(),
        OutreachEngagementCollector(),
        ReconFindingsCollector(),
        MemoryBacklogCollector(),
        BudgetCollector(),
        ErrorSpikeCollector(),
        CriticalFailureCollector(),
        StrategicTimerCollector(),
    ]
    readings = await collect_all(collectors)
    assert len(readings) == 9
    assert all(isinstance(r, SignalReading) for r in readings)


async def test_collect_all_tolerates_failure():
    """A failing collector should not break collect_all."""

    class FailingCollector:
        signal_name = "broken"

        async def collect(self) -> SignalReading:
            raise RuntimeError("boom")

    collectors = [ConversationCollector(), FailingCollector()]
    readings = await collect_all(collectors)
    # Should get 2 readings: one real, one fallback 0.0 for the failure
    assert len(readings) == 2
    values = {r.name: r.value for r in readings}
    assert values["broken"] == 0.0
    assert values["conversations_since_reflection"] == 0.0
