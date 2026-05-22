"""Tests for Light reflection focus area gating.

Anomaly focus is event-driven — falls back to situation when no critical
operational signals are active.
"""

from genesis.awareness.types import Depth, DepthScore, SignalReading, TickResult
from genesis.cc.reflection_bridge._prompts import _light_focus_area


def _make_tick(signals: list[SignalReading], tick_id: str = "00000000-0000-0000-0000-000000000002") -> TickResult:
    """Build a TickResult for focus area testing.

    Default tick_id has UUID int % 3 == 2, which maps to 'anomaly' in rotation.
    """
    return TickResult(
        tick_id=tick_id,
        timestamp="2026-05-22T12:00:00+00:00",
        source="scheduled",
        signals=signals,
        scores=[DepthScore(
            depth=Depth.LIGHT, raw_score=0.8,
            time_multiplier=1.0, final_score=0.8,
            threshold=0.6, triggered=True,
        )],
        classified_depth=Depth.LIGHT,
        trigger_reason="threshold_exceeded",
    )


def test_anomaly_fires_on_error_spike():
    """Anomaly focus fires when software_error_spike > 0."""
    tick = _make_tick([
        SignalReading(name="software_error_spike", value=0.5, source="test",
                      collected_at="2026-05-22T12:00:00+00:00"),
    ])
    assert _light_focus_area(tick) == "anomaly"


def test_anomaly_fires_on_critical_failure():
    """Anomaly focus fires when critical_failure > 0."""
    tick = _make_tick([
        SignalReading(name="critical_failure", value=1.0, source="test",
                      collected_at="2026-05-22T12:00:00+00:00"),
    ])
    assert _light_focus_area(tick) == "anomaly"


def test_anomaly_fires_on_sentinel_high():
    """Anomaly focus fires when sentinel_activity >= 0.7."""
    tick = _make_tick([
        SignalReading(name="sentinel_activity", value=0.7, source="test",
                      collected_at="2026-05-22T12:00:00+00:00"),
    ])
    assert _light_focus_area(tick) == "anomaly"


def test_anomaly_falls_back_on_routine_signals():
    """Anomaly focus falls back to situation when no critical signals active."""
    tick = _make_tick([
        SignalReading(name="autonomy_activity", value=0.3, source="test",
                      collected_at="2026-05-22T12:00:00+00:00"),
        SignalReading(name="container_memory_pct", value=0.25, source="test",
                      collected_at="2026-05-22T12:00:00+00:00"),
    ])
    assert _light_focus_area(tick) == "situation"


def test_anomaly_falls_back_on_low_sentinel():
    """Anomaly doesn't fire when sentinel_activity < 0.7."""
    tick = _make_tick([
        SignalReading(name="sentinel_activity", value=0.3, source="test",
                      collected_at="2026-05-22T12:00:00+00:00"),
    ])
    assert _light_focus_area(tick) == "situation"


def test_situation_focus_unaffected():
    """Situation focus always fires regardless of signals."""
    # tick_id with UUID int % 3 == 0 maps to 'situation'
    tick = _make_tick(
        [SignalReading(name="autonomy_activity", value=0.3, source="test",
                       collected_at="2026-05-22T12:00:00+00:00")],
        tick_id="00000000-0000-0000-0000-000000000000",
    )
    assert _light_focus_area(tick) == "situation"


def test_user_impact_focus_unaffected():
    """User impact focus always fires regardless of signals."""
    # tick_id with UUID int % 3 == 1 maps to 'user_impact'
    tick = _make_tick(
        [SignalReading(name="autonomy_activity", value=0.3, source="test",
                       collected_at="2026-05-22T12:00:00+00:00")],
        tick_id="00000000-0000-0000-0000-000000000001",
    )
    assert _light_focus_area(tick) == "user_impact"
