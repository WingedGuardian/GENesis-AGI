"""Tests for ProbeTransitionTracker — boundary crossings + flap + warmup."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from genesis.observability.probe_transitions import ProbeTransitionTracker


class _Clock:
    """Controllable clock; advance() moves virtual time forward."""

    def __init__(self, start: datetime) -> None:
        self._now = start

    def __call__(self) -> datetime:
        return self._now

    def advance(self, **kw) -> None:
        self._now = self._now + timedelta(**kw)


def _tracker(clock, *, warmup_s: int = 0) -> ProbeTransitionTracker:
    # warmup 0 by default so tests exercise emits immediately unless stated
    return ProbeTransitionTracker(clock=clock, warmup=timedelta(seconds=warmup_s))


def test_first_observation_seeds_no_emit():
    clk = _Clock(datetime(2026, 1, 1, tzinfo=UTC))
    t = _tracker(clk)
    assert t.observe("genesis.db", "healthy") is None


def test_healthy_to_down_emits_one_transition():
    clk = _Clock(datetime(2026, 1, 1, tzinfo=UTC))
    t = _tracker(clk)
    t.observe("genesis.db", "healthy")
    clk.advance(minutes=5)
    tr = t.observe("genesis.db", "down")
    assert tr is not None
    assert tr.probe_id == "genesis.db"
    assert tr.old_class == "healthy" and tr.new_class == "unhealthy"
    assert tr.old_status == "healthy" and tr.new_status == "down"
    assert tr.flapping is False


def test_recovery_emits_transition():
    clk = _Clock(datetime(2026, 1, 1, tzinfo=UTC))
    t = _tracker(clk)
    t.observe("qdrant", "healthy")
    clk.advance(minutes=5)
    t.observe("qdrant", "error")
    clk.advance(minutes=5)
    tr = t.observe("qdrant", "healthy")
    assert tr is not None
    assert tr.old_class == "unhealthy" and tr.new_class == "healthy"


def test_same_class_no_emit():
    """degraded and down are both 'unhealthy' — no crossing between them."""
    clk = _Clock(datetime(2026, 1, 1, tzinfo=UTC))
    t = _tracker(clk)
    t.observe("guardian", "healthy")
    clk.advance(minutes=1)
    assert t.observe("guardian", "degraded") is not None  # healthy->unhealthy
    clk.advance(minutes=1)
    assert t.observe("guardian", "down") is None  # unhealthy->unhealthy, no cross


def test_unknown_is_noop_not_a_crossing():
    """A healthy->unknown->healthy flicker must emit nothing (no-signal state)."""
    clk = _Clock(datetime(2026, 1, 1, tzinfo=UTC))
    t = _tracker(clk)
    t.observe("scheduler", "healthy")
    clk.advance(minutes=1)
    assert t.observe("scheduler", "unknown") is None  # ignored, class unchanged
    clk.advance(minutes=1)
    assert t.observe("scheduler", "healthy") is None  # still healthy, no crossing


def test_unavailable_does_not_seed():
    """First-ever observation of a no-signal status must not seed; a later real
    status is then the seed (no bogus transition)."""
    clk = _Clock(datetime(2026, 1, 1, tzinfo=UTC))
    t = _tracker(clk)
    assert t.observe("qdrant_collections", "unavailable") is None
    clk.advance(minutes=1)
    # first real status seeds, still no emit
    assert t.observe("qdrant_collections", "healthy") is None
    clk.advance(minutes=1)
    assert t.observe("qdrant_collections", "down") is not None


def test_flapping_flagged_after_threshold():
    clk = _Clock(datetime(2026, 1, 1, tzinfo=UTC))
    t = _tracker(clk)
    t.observe("cc_tmp", "healthy")  # seed
    flags = []
    # 6 crossings within 15 min: healthy<->down repeatedly
    for i in range(6):
        clk.advance(minutes=1)
        status = "down" if i % 2 == 0 else "healthy"
        tr = t.observe("cc_tmp", status)
        assert tr is not None
        flags.append(tr.flapping)
    # threshold is 3 crossings in-window; the 4th+ crossing is flagged
    assert flags[:3] == [False, False, False]
    assert all(flags[3:])


def test_flap_window_resets_after_quiet_period():
    clk = _Clock(datetime(2026, 1, 1, tzinfo=UTC))
    t = _tracker(clk)
    t.observe("container_memory", "healthy")
    for i in range(4):
        clk.advance(minutes=1)
        t.observe("container_memory", "down" if i % 2 == 0 else "healthy")
    # long quiet gap flushes the window
    clk.advance(minutes=30)
    tr = t.observe("container_memory", "down")
    assert tr is not None
    assert tr.flapping is False


def test_warmup_suppresses_emit_but_tracks_state():
    clk = _Clock(datetime(2026, 1, 1, tzinfo=UTC))
    t = ProbeTransitionTracker(clock=clk, warmup=timedelta(seconds=90))
    t.observe("genesis.db", "healthy")  # seed at t0
    clk.advance(seconds=30)
    # crossing inside warmup: state updates, but no emit
    assert t.observe("genesis.db", "down") is None
    clk.advance(seconds=120)  # now past warmup
    # class is already 'unhealthy' from the suppressed crossing; recovery emits
    tr = t.observe("genesis.db", "healthy")
    assert tr is not None
    assert tr.old_class == "unhealthy" and tr.new_class == "healthy"
