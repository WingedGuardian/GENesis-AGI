"""Tests for the AwarenessLoop orchestrator.

Tests the tick pipeline directly (perform_tick) without relying on
APScheduler timing. Scheduler integration is tested separately.
"""

import json
from unittest.mock import AsyncMock, MagicMock

from genesis.awareness.loop import AwarenessLoop, perform_tick
from genesis.awareness.signals import ConversationCollector
from genesis.awareness.types import Depth, DepthScore, SignalReading, TickResult
from genesis.db.crud import awareness_ticks, observations
from genesis.observability.types import Subsystem


async def _persist_tick(db, tick: TickResult) -> None:
    """Persist a TickResult to the DB so mark_dispatched can find it."""
    await awareness_ticks.create(
        db,
        id=tick.tick_id,
        source=tick.source,
        signals_json=json.dumps([{"name": s.name, "value": s.value} for s in tick.signals]),
        scores_json=json.dumps({}),
        created_at=tick.timestamp,
        classified_depth=tick.classified_depth.value if tick.classified_depth else None,
        trigger_reason=tick.trigger_reason,
    )


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


def _micro_tick(signals: list[SignalReading], tick_id: str = "test-tick") -> TickResult:
    """Helper: build a TickResult classified as Micro with given signals."""
    return TickResult(
        tick_id=tick_id,
        timestamp="2026-05-21T12:00:00+00:00",
        source="scheduled",
        signals=signals,
        scores=[DepthScore(
            depth=Depth.MICRO, raw_score=0.5,
            time_multiplier=1.0, final_score=0.5,
            threshold=0.3, triggered=True,
        )],
        classified_depth=Depth.MICRO,
        trigger_reason="threshold_exceeded",
    )


async def test_micro_dispatch_silent_on_routine_signals(db):
    """Micro ticks with only routine signals skip LLM — mark dispatched, no engine call."""
    engine = AsyncMock()
    loop = AwarenessLoop(db=db, collectors=[])
    loop.set_reflection_engine(engine)

    tick = _micro_tick([
        SignalReading(name="autonomy_activity", value=0.3, source="test",
                      collected_at="2026-05-21T12:00:00+00:00"),
        SignalReading(name="surplus_activity", value=0.0, source="test",
                      collected_at="2026-05-21T12:00:00+00:00"),
    ])

    # Persist the tick so mark_dispatched can find it
    await _persist_tick(db, tick)

    await loop._dispatch_reflection(tick)

    # Engine should NOT be called — silent path
    engine.reflect.assert_not_called()

    # But tick should be marked dispatched (cascade counting)
    row = await awareness_ticks.get_by_id(db, tick.tick_id)
    assert row["dispatched"] == 1


async def test_micro_dispatch_fires_llm_on_critical_signal(db):
    """Micro ticks with software_error_spike > 0 fire the LLM."""
    engine = AsyncMock()
    engine.reflect = AsyncMock(return_value=MagicMock(success=True, output=None))
    loop = AwarenessLoop(db=db, collectors=[])
    loop.set_reflection_engine(engine)

    tick = _micro_tick([
        SignalReading(name="software_error_spike", value=0.5, source="test",
                      collected_at="2026-05-21T12:00:00+00:00"),
        SignalReading(name="autonomy_activity", value=0.0, source="test",
                      collected_at="2026-05-21T12:00:00+00:00"),
    ])

    await _persist_tick(db, tick)
    await loop._dispatch_reflection(tick)

    # Engine SHOULD be called — critical signal active
    engine.reflect.assert_called_once()
    row = await awareness_ticks.get_by_id(db, tick.tick_id)
    assert row["dispatched"] == 1


async def test_micro_dispatch_fires_llm_on_sentinel_anomaly(db):
    """Micro ticks with sentinel_activity >= 0.7 fire the LLM."""
    engine = AsyncMock()
    engine.reflect = AsyncMock(return_value=MagicMock(success=True, output=None))
    loop = AwarenessLoop(db=db, collectors=[])
    loop.set_reflection_engine(engine)

    tick = _micro_tick([
        SignalReading(name="sentinel_activity", value=0.7, source="test",
                      collected_at="2026-05-21T12:00:00+00:00"),
    ])

    await _persist_tick(db, tick)
    await loop._dispatch_reflection(tick)

    engine.reflect.assert_called_once()


async def test_micro_dispatch_silent_on_low_sentinel(db):
    """Micro ticks with sentinel_activity < 0.7 do NOT fire LLM."""
    engine = AsyncMock()
    loop = AwarenessLoop(db=db, collectors=[])
    loop.set_reflection_engine(engine)

    tick = _micro_tick([
        SignalReading(name="sentinel_activity", value=0.3, source="test",
                      collected_at="2026-05-21T12:00:00+00:00"),
    ])

    await _persist_tick(db, tick)
    await loop._dispatch_reflection(tick)

    engine.reflect.assert_not_called()


async def test_micro_dispatch_fires_llm_on_critical_failure(db):
    """Micro ticks with critical_failure > 0 fire the LLM."""
    engine = AsyncMock()
    engine.reflect = AsyncMock(return_value=MagicMock(success=True, output=None))
    loop = AwarenessLoop(db=db, collectors=[])
    loop.set_reflection_engine(engine)

    tick = _micro_tick([
        SignalReading(name="critical_failure", value=1.0, source="test",
                      collected_at="2026-05-21T12:00:00+00:00"),
    ], tick_id="test-crit-fail")

    await _persist_tick(db, tick)
    await loop._dispatch_reflection(tick)

    engine.reflect.assert_called_once()


# ── depth=None idle alive-pulse (keep reflection heartbeat fresh when quiet) ──


def _idle_tick(*, db_available: bool = True, tick_id: str = "idle-tick") -> TickResult:
    """Helper: a quiet tick that classified to depth=None (no reflection ran)."""
    return TickResult(
        tick_id=tick_id,
        timestamp="2026-06-20T04:00:00+00:00",
        source="scheduled",
        signals=[],
        scores=[],
        classified_depth=None,
        trigger_reason="no trigger",
        db_available=db_available,
    )


async def test_idle_heartbeat_helper_emits_when_healthy(db):
    """The idle-pulse helper emits a REFLECTION heartbeat for a healthy
    depth=None tick so subsystem_heartbeats does not falsely report 'dark'."""
    event_bus = MagicMock()
    event_bus.emit = AsyncMock()
    loop = AwarenessLoop(db=db, collectors=[], event_bus=event_bus)

    await loop._emit_reflection_idle_heartbeat(_idle_tick())

    event_bus.emit.assert_called_once()
    call_args = event_bus.emit.call_args.args
    assert call_args[0] == Subsystem.REFLECTION
    assert call_args[2] == "heartbeat"


async def test_idle_heartbeat_helper_skips_degraded_tick(db):
    """A DEGRADED tick (db_available=False) must NOT pulse — a genuine
    reflection outage should still age out and alarm rather than be masked."""
    event_bus = MagicMock()
    event_bus.emit = AsyncMock()
    loop = AwarenessLoop(db=db, collectors=[], event_bus=event_bus)

    await loop._emit_reflection_idle_heartbeat(
        _idle_tick(db_available=False, tick_id="degraded-tick"),
    )

    event_bus.emit.assert_not_called()


async def test_on_tick_pulses_reflection_heartbeat_when_idle(db, monkeypatch):
    """Wiring: a real quiet tick (no signals → depth=None) routes through
    _on_tick to the idle pulse and emits exactly one REFLECTION heartbeat.

    This guards against the pulse being placed on a path _on_tick never calls
    (e.g. inside _dispatch_reflection, which only runs when depth is non-None)."""
    event_bus = MagicMock()
    event_bus.emit = AsyncMock()
    loop = AwarenessLoop(db=db, collectors=[], event_bus=event_bus)

    # Record out-of-band dispatches without running them (deferred-retry etc.).
    def _fake_tracked_task(coro, *, name="", **kw):
        coro.close()
        return None

    monkeypatch.setattr("genesis.util.tasks.tracked_task", _fake_tracked_task)

    await loop._on_tick()

    reflection_beats = [
        c for c in event_bus.emit.call_args_list
        if c.args and c.args[0] == Subsystem.REFLECTION and c.args[2] == "heartbeat"
    ]
    assert len(reflection_beats) == 1
