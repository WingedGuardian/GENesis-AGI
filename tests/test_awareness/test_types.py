"""Tests for awareness loop data types."""

from genesis.awareness.types import Depth, DepthScore, SignalReading, TickResult


def test_depth_enum_values():
    """Depth names must match DB seed data exactly."""
    assert Depth.MICRO.value == "Micro"
    assert Depth.LIGHT.value == "Light"
    assert Depth.DEEP.value == "Deep"
    assert Depth.STRATEGIC.value == "Strategic"


def test_depth_enum_all_values():
    assert len(Depth) == 4


def test_signal_reading_creation():
    sr = SignalReading(
        name="software_error_spike",
        value=0.8,
        source="health_mcp",
        collected_at="2026-03-03T12:00:00+00:00",
    )
    assert sr.name == "software_error_spike"
    assert sr.value == 0.8


def test_signal_reading_immutable():
    import pytest

    sr = SignalReading(
        name="test", value=0.5, source="test", collected_at="2026-03-03T12:00:00+00:00"
    )
    with pytest.raises(AttributeError):
        sr.value = 0.9


def test_depth_score_triggered():
    ds = DepthScore(
        depth=Depth.MICRO,
        raw_score=0.6,
        time_multiplier=1.0,
        final_score=0.6,
        threshold=0.5,
        triggered=True,
    )
    assert ds.triggered is True


def test_tick_result_no_trigger():
    tr = TickResult(
        tick_id="t-001",
        timestamp="2026-03-03T12:00:00+00:00",
        source="scheduled",
        signals=[],
        scores=[],
        classified_depth=None,
        trigger_reason=None,
    )
    assert tr.classified_depth is None
