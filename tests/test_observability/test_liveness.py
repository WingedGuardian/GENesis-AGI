"""Tests for the pure pulse-liveness verdict (genesis.observability.liveness).

Stalled = a scheduled job whose success PULSE stopped — its last completed cycle
(job_health.last_success) is older than the conservative threshold AND the system
is not paused. This is the NOW-vs-last_success model, for jobs (surplus_dispatch,
drainers) that record success every cycle unconditionally — distinct from the ego's
intent-vs-completion lag (genesis.ego.liveness). A never-succeeded job (last_success
None) is NOT stalled here (the job_never_succeeded alarm owns that case); a paused
system is never stalled (its pulse legitimately freezes).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from genesis.observability.liveness import (
    STALL_FLOOR_MINUTES,
    compute_pulse_liveness,
    stall_threshold_minutes,
)

NOW = datetime(2026, 8, 24, 12, 0, 0, tzinfo=UTC)


def _iso(minutes_ago: float) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).isoformat()


def _live(*, success_min_ago, interval=5, paused=False):
    return compute_pulse_liveness(
        last_success_at=None if success_min_ago is None else _iso(success_min_ago),
        expected_interval_minutes=interval,
        paused=paused,
        now=NOW,
    )


def test_never_succeeded_is_not_stalled():
    """No last_success (fresh install / never once succeeded) → not stalled;
    the job_never_succeeded alarm owns that case, not this verdict."""
    assert _live(success_min_ago=None).stalled is False


def test_recent_success_is_not_stalled():
    """A success within the threshold → healthy."""
    assert _live(success_min_ago=5).stalled is False


def test_stale_success_not_paused_is_stalled():
    """last_success far past the threshold and NOT paused → stalled (the wedged
    dispatch loop the idle-proxy status hides)."""
    v = _live(success_min_ago=5 * 60)  # 5h > 3h floor
    assert v.stalled is True
    assert v.reason and "no completed cycle" in v.reason


def test_stale_success_but_paused_is_not_stalled():
    """A globally-paused system legitimately freezes the success pulse — never a
    stall, even 5h stale. This is the pause-exclusion the surplus loop requires."""
    assert _live(success_min_ago=5 * 60, paused=True).stalled is False


def test_transient_under_threshold_not_stalled():
    """A single missed cycle (well under the 3h floor) is not a stall."""
    assert _live(success_min_ago=60).stalled is False


def test_floor_dominates_short_cadence():
    """A 5-min cadence collapses to the 3h floor (4*5=20m < 180m), so surplus
    only reds after 3h of no success — near-zero false-red."""
    assert stall_threshold_minutes(5) == STALL_FLOOR_MINUTES
    # 2h49m stale is still under the floor.
    assert _live(success_min_ago=169).stalled is False
    # 3h1m stale trips it.
    assert _live(success_min_ago=181).stalled is True


def test_degenerate_interval_uses_floor():
    """0 / None / negative interval collapse to the floor, never a tiny threshold."""
    assert stall_threshold_minutes(0) == STALL_FLOOR_MINUTES
    assert stall_threshold_minutes(-5) == STALL_FLOOR_MINUTES


def test_materially_future_pulse_is_not_confirmable():
    """A pulse well in the FUTURE (clock stepped backward / corrupt data) is not a
    confirmable recent success: overdue_minutes is None (caller → unavailable), and
    NEVER a green from the negative age. Without this it would read healthy and hide a
    wedged scheduler until now catches up."""
    v = _live(success_min_ago=-30)  # 30 min in the future
    assert v.stalled is False
    assert v.overdue_minutes is None


def test_small_future_skew_is_tolerated():
    """A pulse a couple minutes ahead (sub-tolerance clock jitter) is still 'recent' →
    healthy with a real (negative) overdue, not forced unavailable."""
    v = _live(success_min_ago=-2)  # 2 min future, within the 5m tolerance
    assert v.stalled is False
    assert v.overdue_minutes is not None


def test_large_interval_scales_threshold():
    """A slow job (interval*4 > floor) needs a proportionally larger gap → a
    legitimately slow cadence never false-reds."""
    assert stall_threshold_minutes(120) == 120 * 4  # 480m > 180m floor
    assert _live(success_min_ago=300, interval=120).stalled is False  # 5h < 8h
