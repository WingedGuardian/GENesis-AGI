"""Tests for the pure ego-liveness verdict (genesis.ego.liveness).

Stalled = the ego's last INTENT to cycle (last_proactive_fire_at) leads its last
COMPLETED cycle (job_health.last_success) by more than the threshold — i.e. it is
actively trying but not completing. Suppression (idle/quiet/paused/circuit) never
records an intent, so it is excluded by construction.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from genesis.ego.liveness import (
    STALL_FLOOR_MINUTES,
    compute_ego_liveness,
    stall_threshold_minutes,
)

NOW = datetime(2026, 8, 21, 12, 0, 0, tzinfo=UTC)


def _iso(minutes_ago: float) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).isoformat()


def _live(
    *,
    success_min_ago,
    intent_min_ago,
    interval=90,
    gated=False,
    gated_min_ago=None,
):
    return compute_ego_liveness(
        last_success_at=None if success_min_ago is None else _iso(success_min_ago),
        last_intent_at=None if intent_min_ago is None else _iso(intent_min_ago),
        current_interval_minutes=interval,
        gated=gated,
        last_gated_at=None if gated_min_ago is None else _iso(gated_min_ago),
        now=NOW,
    )


def test_no_intent_is_never_stalled():
    """Fresh install / an ego that has never proactively tried → never stalled."""
    assert _live(success_min_ago=3 * 24 * 60, intent_min_ago=None).stalled is False


def test_no_success_is_never_stalled():
    """No completed cycle yet (fresh install) → never stalled."""
    assert _live(success_min_ago=None, intent_min_ago=5).stalled is False


def test_healthy_completion_after_intent():
    """Completed since the last recorded intent (lag negative) → not stalled."""
    v = _live(success_min_ago=50, intent_min_ago=60)  # success 10m after intent
    assert v.stalled is False


def test_deadlock_intent_recent_completion_old_is_stalled():
    """Actively pushing (recent intent) but no completion in 3 days → stalled."""
    v = _live(success_min_ago=3 * 24 * 60, intent_min_ago=5)
    assert v.stalled is True
    assert v.reason and "actively cycling" in v.reason


def test_gated_is_not_stalled():
    """A pending approval (intent recorded, legitimately awaiting the user) is
    not a stall."""
    v = _live(success_min_ago=3 * 24 * 60, intent_min_ago=5, gated=True)
    assert v.stalled is False


def test_suppressed_ego_stopped_trying_is_not_stalled():
    """The convergence case: 15h since the last cycle, but the ego also stopped
    TRYING (intent is old too — idle/quiet/paused suppressed the push). Lag is
    small → not stalled. No enumeration of suppression conditions needed."""
    v = _live(success_min_ago=15 * 60 + 30, intent_min_ago=15 * 60)
    assert v.stalled is False


def test_transient_lag_under_threshold_not_stalled():
    """A single missed completion (lag ~1 interval) is under the floor → not
    stalled; only a sustained lag trips."""
    v = _live(success_min_ago=90 + 90, intent_min_ago=90, interval=90)  # lag 90m
    assert v.stalled is False


def test_floor_applies_to_short_cadence():
    """A 90m interval yields a 6h threshold (4*90m=360m dominates the 3h floor);
    the floor only wins for cadences shorter than 45m."""
    assert stall_threshold_minutes(90) == max(90 * 4, STALL_FLOOR_MINUTES)


def test_threshold_scales_with_large_backoff_interval():
    """A backed-off ego (24h interval) needs a 96h lag to be stalled — a
    legitimate slow cadence never false-reds."""
    interval = 24 * 60
    assert stall_threshold_minutes(interval) == interval * 4
    v = _live(success_min_ago=3 * 24 * 60, intent_min_ago=5, interval=interval)
    assert v.stalled is False  # 3-day lag < 96h threshold


def test_degenerate_interval_uses_floor():
    """0/None/negative interval collapse to the hard floor, never a tiny
    threshold."""
    assert stall_threshold_minutes(0) == STALL_FLOOR_MINUTES
    assert stall_threshold_minutes(-5) == STALL_FLOOR_MINUTES


# ---------------------------------------------------------------------------
# Grace after gate-release — the CLI-approval-release race (C5b)
#
# A long CLI-approval hold suppresses stalled via `gated` while it lasts. The
# false-positive is the RACE at release: `gated` flips False the instant the
# approval resolves, but `last_success` can't advance until the now-unblocked
# cycle completes (dispatch + runtime). In that window: not-gated + stale
# completion + old intent → a false "stalled". Grace = one cadence interval
# after the ego was last gate-held covers the release→completion gap; a
# never-gated real deadlock is unaffected.
# ---------------------------------------------------------------------------


def test_recent_gate_release_not_stalled():
    """Release race: approval just resolved (gated_min_ago=1), completion still
    stale (6h), intent recent — WITHOUT the grace this reads stalled. Within one
    interval of the gate release → NOT stalled."""
    v = _live(
        success_min_ago=6 * 60 + 30,
        intent_min_ago=5,
        interval=90,
        gated_min_ago=1,
    )
    assert v.stalled is False


def test_gate_release_grace_expired_is_stalled():
    """Long after the gate released (2 intervals), a still-stale completion is a
    genuine stall — grace does not mask a real problem indefinitely."""
    v = _live(
        success_min_ago=6 * 60 + 30,
        intent_min_ago=5,
        interval=90,
        gated_min_ago=180,
    )
    assert v.stalled is True
    assert v.reason and "actively cycling" in v.reason


def test_never_gated_deadlock_still_stalled():
    """A real deadlock (never gate-held: last_gated_at None) still fires — the
    grace only applies to a recently-gated ego."""
    v = _live(
        success_min_ago=3 * 24 * 60,
        intent_min_ago=5,
        gated_min_ago=None,
    )
    assert v.stalled is True


def test_gate_release_grace_scales_with_interval():
    """Grace tracks the cadence interval: the genesis ego (240m) gets a 240m
    grace, so a release 3h ago is still within grace."""
    v = _live(
        success_min_ago=20 * 60,
        intent_min_ago=5,
        interval=240,
        gated_min_ago=180,
    )
    assert v.stalled is False  # 180m since release < 240m grace


def test_recent_gate_release_healthy_stays_healthy():
    """Grace never FLIPS a healthy ego to stalled — a recently-gated ego that
    also completed recently is simply not stalled (lag small)."""
    v = _live(
        success_min_ago=40,
        intent_min_ago=50,
        interval=90,
        gated_min_ago=1,
    )
    assert v.stalled is False


def test_future_last_gated_fails_toward_detection():
    """A last_gated in the FUTURE (clock skew / corrupt row) must NOT grant
    grace — a safety monitor fails toward detection, so a real stall fires."""
    v = _live(
        success_min_ago=3 * 24 * 60,
        intent_min_ago=5,
        interval=90,
        gated_min_ago=-30,  # 30m in the FUTURE
    )
    assert v.stalled is True


def test_grace_floor_protects_short_cadence():
    """A 30m cadence would give a 30m grace — shorter than a cycle can take. The
    floor (60m) protects it: a release 45m ago is still within grace, one 70m ago
    (past the floor) is a genuine stall."""
    within = _live(
        success_min_ago=6 * 60 + 30,
        intent_min_ago=5,
        interval=30,
        gated_min_ago=45,
    )
    assert within.stalled is False  # 45m < 60m floor
    expired = _live(
        success_min_ago=6 * 60 + 30,
        intent_min_ago=5,
        interval=30,
        gated_min_ago=70,
    )
    assert expired.stalled is True  # 70m > 60m floor


def test_degenerate_interval_with_gate_release_uses_floor():
    """A degenerate (0) interval + last_gated collapses grace to the floor, not
    zero — a recently-released ego is still excused, never false-fired."""
    v = _live(
        success_min_ago=6 * 60 + 30,
        intent_min_ago=5,
        interval=0,
        gated_min_ago=30,
    )
    assert v.stalled is False  # 30m < 60m floor (interval 0 → floor)
