"""Tests for the pure ego-liveness verdict (genesis.ego.liveness)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from genesis.ego.liveness import (
    STALL_FLOOR_MINUTES,
    compute_ego_liveness,
    quiet_suppress_floor_minutes,
    stall_threshold_minutes,
)

NOW = datetime(2026, 8, 21, 12, 0, 0, tzinfo=UTC)


def _iso(minutes_ago: float) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).isoformat()


def test_no_last_success_is_never_stalled():
    """Fresh install (no completed cycle) must never read stalled."""
    v = compute_ego_liveness(
        last_success_at=None,
        current_interval_minutes=90,
        gated=False,
        is_paused=False,
        now=NOW,
    )
    assert v.stalled is False
    assert v.last_success_at is None
    assert v.overdue_minutes is None


def test_recent_cycle_not_stalled():
    v = compute_ego_liveness(
        last_success_at=_iso(30),
        current_interval_minutes=90,
        gated=False,
        is_paused=False,
        now=NOW,
    )
    assert v.stalled is False


def test_overdue_cycle_is_stalled_with_reason():
    """3 days silent on a 90m cadence, not gated/paused → stalled."""
    v = compute_ego_liveness(
        last_success_at=_iso(3 * 24 * 60),
        current_interval_minutes=90,
        gated=False,
        is_paused=False,
        now=NOW,
    )
    assert v.stalled is True
    assert v.reason and "no completed cycle" in v.reason


def test_gated_is_not_stalled():
    """A pending approval (gate on) is a legitimate wait, not a stall."""
    v = compute_ego_liveness(
        last_success_at=_iso(3 * 24 * 60),
        current_interval_minutes=90,
        gated=True,
        is_paused=False,
        now=NOW,
    )
    assert v.stalled is False


def test_paused_is_not_stalled():
    v = compute_ego_liveness(
        last_success_at=_iso(3 * 24 * 60),
        current_interval_minutes=90,
        gated=False,
        is_paused=True,
        now=NOW,
    )
    assert v.stalled is False


def test_floor_protects_short_cadence_from_quiet_hours():
    """A 90m ego silent 8h (an overnight quiet window) is under the 12h floor →
    not stalled. Conservative: no false red on a legitimate lull."""
    v = compute_ego_liveness(
        last_success_at=_iso(8 * 60),
        current_interval_minutes=90,
        gated=False,
        is_paused=False,
        now=NOW,
    )
    assert v.stalled is False
    assert v.threshold_minutes == STALL_FLOOR_MINUTES  # 12h dominates 4*90m


def test_quiet_suppress_floor_prevents_false_red():
    """A quiet-hours SUPPRESS window longer than the 12h floor must raise the
    threshold above the window, so a legitimately-suppressed ego is not flagged."""
    # 22:00 -> 18:00 = a 20h suppress window.
    floor = quiet_suppress_floor_minutes(
        mode="suppress",
        start_hour=22,
        end_hour=18,
        interval_minutes=90,
    )
    assert floor == 20 * 60 + 90  # window span + one interval of slack
    v = compute_ego_liveness(
        last_success_at=_iso(19 * 60),
        current_interval_minutes=90,
        gated=False,
        is_paused=False,
        quiet_floor_minutes=floor,
        now=NOW,
    )
    assert v.stalled is False  # 19h silence < ~21.5h threshold


def test_floor_mode_needs_no_quiet_floor():
    """In 'floor' mode ticks still fire (throttled), so no floor is applied."""
    assert (
        quiet_suppress_floor_minutes(
            mode="floor",
            start_hour=22,
            end_hour=8,
            interval_minutes=90,
        )
        == 0.0
    )


def test_threshold_scales_with_large_backoff_interval():
    """A backed-off ego (24h interval) is NOT stalled at 3 days — 4*24h=96h
    threshold dominates the floor, so a legitimate slow cadence never false-reds."""
    interval = 24 * 60  # 24h
    assert stall_threshold_minutes(interval) == interval * 4
    v = compute_ego_liveness(
        last_success_at=_iso(3 * 24 * 60),
        current_interval_minutes=interval,
        gated=False,
        is_paused=False,
        now=NOW,
    )
    assert v.stalled is False
