"""Tests for IdleDetector."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from genesis.surplus.idle_detector import IdleDetector


def _make_clock(dt: datetime):
    """Return a callable clock returning dt, advanceable via .advance()."""
    state = {"now": dt}

    def clock():
        return state["now"]

    clock.advance = lambda minutes: state.update(now=state["now"] + timedelta(minutes=minutes))
    return clock


NOW = datetime(2026, 3, 4, 12, 0, 0, tzinfo=UTC)


def test_starts_idle():
    d = IdleDetector()
    assert d.is_idle() is True


def test_not_idle_after_activity():
    d = IdleDetector()
    d.mark_active()
    assert d.is_idle() is False


def test_idle_after_threshold():
    clock = _make_clock(NOW)
    d = IdleDetector(clock=clock)
    d.mark_active()
    clock.advance(20)
    assert d.is_idle(threshold_minutes=15) is True


def test_not_idle_within_threshold():
    clock = _make_clock(NOW)
    d = IdleDetector(clock=clock)
    d.mark_active()
    clock.advance(10)
    assert d.is_idle(threshold_minutes=15) is False


def test_idle_since_returns_none_when_active():
    clock = _make_clock(NOW)
    d = IdleDetector(clock=clock)
    d.mark_active()
    clock.advance(5)
    assert d.idle_since(threshold_minutes=15) is None


def test_idle_since_returns_timestamp():
    clock = _make_clock(NOW)
    d = IdleDetector(clock=clock)
    d.mark_active()
    activity_time = clock()
    clock.advance(20)
    assert d.idle_since(threshold_minutes=15) == activity_time


def test_idle_since_none_when_never_active():
    d = IdleDetector()
    assert d.idle_since() is None


def test_mark_active_updates_timestamp():
    clock = _make_clock(NOW)
    d = IdleDetector(clock=clock)
    d.mark_active()
    first = d._last_activity_at
    clock.advance(5)
    d.mark_active()
    assert d._last_activity_at > first


def test_custom_clock():
    clock = _make_clock(NOW)
    d = IdleDetector(clock=clock)
    d.mark_active()
    assert d._last_activity_at == NOW
    clock.advance(10)
    d.mark_active()
    assert d._last_activity_at == NOW + timedelta(minutes=10)


def test_runtime_exposes_idle_detector():
    """GenesisRuntime has an idle_detector property (None before bootstrap)."""
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.__new__(GenesisRuntime)
    rt._idle_detector = None
    assert rt.idle_detector is None

    d = IdleDetector()
    rt._idle_detector = d
    assert rt.idle_detector is d


def test_runtime_idle_detector_mark_active_affects_is_idle():
    """mark_active() via runtime property transitions idle state."""
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.__new__(GenesisRuntime)
    d = IdleDetector()
    rt._idle_detector = d

    # Before mark_active, starts idle (no activity recorded)
    assert d.is_idle() is True

    rt.idle_detector.mark_active()
    assert d.is_idle() is False
