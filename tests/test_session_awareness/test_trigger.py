"""Trigger unit tests — pure function, time passed as a parameter."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from genesis.session_awareness.statefiles import empty_state
from genesis.session_awareness.trigger import (
    EMA_MIN_TURNS,
    MAX_FIRES,
    TURNS_BETWEEN_FIRES,
    check_fire,
    record_fire,
    stability,
)

DIM = 8
NOW = datetime(2026, 7, 9, 12, 0, 0, tzinfo=UTC)
NOW_ISO = NOW.isoformat()


def unit(axis: int) -> list[float]:
    v = [0.0] * DIM
    v[axis] = 1.0
    return v


def blend(a: list[float], b: list[float], t: float) -> list[float]:
    return [(1 - t) * x + t * y for x, y in zip(a, b, strict=True)]


def settled_state(axis: int = 0, turns: int = 4) -> dict:
    """A state whose theme has settled on the given axis."""
    s = empty_state("s1")
    s["ema"] = unit(axis)
    s["ema_turns"] = turns
    s["ring"] = [unit(axis), unit(axis), unit(axis)]
    return s


def test_no_fire_without_ema():
    assert check_fire(empty_state("s1"), NOW) == (False, "no_ema")


def test_no_fire_while_warming():
    s = settled_state(turns=EMA_MIN_TURNS - 1)
    assert check_fire(s, NOW) == (False, "warming")


def test_no_fire_when_ring_partial():
    s = settled_state()
    s["ring"] = [unit(0), unit(0)]
    assert check_fire(s, NOW) == (False, "unstable")


def test_no_fire_when_unstable():
    s = settled_state()
    s["ring"] = [unit(0), blend(unit(0), unit(1), 0.5), unit(1)]
    assert check_fire(s, NOW) == (False, "unstable")


def test_fires_when_settled():
    assert check_fire(settled_state(), NOW) == (True, "fire")


def test_record_fire_claims_region():
    s = settled_state()
    record_fire(s, NOW_ISO)
    assert s["fired_count"] == 1
    assert s["fired"][0]["turn"] == s["ema_turns"]
    assert s["fired"][0]["ema"] == unit(0)
    assert s["worker_pending_since"] == NOW_ISO


def test_pending_claim_blocks_then_goes_stale():
    s = settled_state(axis=1, turns=10)
    record_fire(s, NOW_ISO)
    # Move the theme far away and past cooldown so only the claim gates
    s["ema"] = unit(2)
    s["ring"] = [unit(2)] * 3
    s["ema_turns"] = 10 + TURNS_BETWEEN_FIRES
    within = NOW + timedelta(seconds=60)
    assert check_fire(s, within) == (False, "worker_pending")
    after = NOW + timedelta(seconds=121)
    assert check_fire(s, after) == (True, "fire")


def test_unparseable_claim_treated_stale():
    s = settled_state()
    s["worker_pending_since"] = "not-a-timestamp"
    assert check_fire(s, NOW) == (True, "fire")


def test_cooldown_between_fires():
    s = settled_state(turns=5)
    record_fire(s, NOW_ISO)
    s["ema"] = unit(3)
    s["ring"] = [unit(3)] * 3
    s["ema_turns"] = 5 + TURNS_BETWEEN_FIRES - 1
    s["worker_pending_since"] = None
    assert check_fire(s, NOW) == (False, "cooldown")
    s["ema_turns"] = 5 + TURNS_BETWEEN_FIRES
    assert check_fire(s, NOW) == (True, "fire")


def test_near_fired_region_blocks_far_region_fires():
    s = settled_state(turns=5)
    record_fire(s, NOW_ISO)
    s["worker_pending_since"] = None
    s["ema_turns"] = 5 + TURNS_BETWEEN_FIRES
    # Slightly moved theme: distance << 0.35
    near = blend(unit(0), unit(1), 0.1)
    s["ema"] = near
    s["ring"] = [near] * 3
    assert check_fire(s, NOW) == (False, "near_fired_region")
    # Orthogonal theme: distance 1.0
    s["ema"] = unit(4)
    s["ring"] = [unit(4)] * 3
    assert check_fire(s, NOW) == (True, "fire")


def test_max_fires_cap():
    s = settled_state(turns=50)
    s["fired_count"] = MAX_FIRES
    assert check_fire(s, NOW) == (False, "max_fires")


def test_stability_empty_ring():
    assert stability([]) == 0.0
    assert stability([unit(0)]) == 0.0
