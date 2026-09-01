"""Unit tests for the loop-health publish/read seam (``genesis.util.loop_health``).

The module's integration behavior is covered via the sampler
(``tests/test_hosting/test_loop_lag_sampler.py``) and the sync liveness route
(``tests/test_dashboard/test_liveness_route.py``); these pin the module's own
contract directly — above all the ``age_s`` clamp and ``now=`` override, which
the integration tests never reach.
"""

from __future__ import annotations

import time

import pytest

from genesis.util import loop_health
from genesis.util.loop_health import LoopHealthSample


def _sample(sampled_monotonic: float = 100.0) -> LoopHealthSample:
    return LoopHealthSample(
        drift_ms=12.5,
        peak_ms=40.0,
        lagging=False,
        threshold_ms=250.0,
        executor={"pending": 0},
        sampled_monotonic=sampled_monotonic,
    )


@pytest.fixture(autouse=True)
def _isolate_module_state(monkeypatch):
    """Reset the module-global latest-sample reference around every test, so
    test order (and the sampler integration tests sharing the process) can
    never leak a sample into these units."""
    monkeypatch.setattr(loop_health, "_latest", None)
    monkeypatch.setattr(loop_health, "_last_lagging_monotonic", None)


def _lagging_sample(sampled_monotonic: float) -> LoopHealthSample:
    return LoopHealthSample(
        drift_ms=3000.0,
        peak_ms=3000.0,
        lagging=True,
        threshold_ms=250.0,
        executor=None,
        sampled_monotonic=sampled_monotonic,
    )


def test_read_before_any_publish_is_none():
    """None = UNKNOWN (sampler never ran) — the fail-closed default."""
    assert loop_health.read() is None


def test_publish_then_read_round_trips_same_object():
    s = _sample()
    loop_health.publish(s)
    assert loop_health.read() is s


def test_publish_replaces_prior_sample():
    first = _sample(sampled_monotonic=100.0)
    second = _sample(sampled_monotonic=200.0)
    loop_health.publish(first)
    loop_health.publish(second)
    assert loop_health.read() is second


def test_sample_is_immutable():
    """The lock-free reader contract relies on the payload never mutating."""
    s = _sample()
    with pytest.raises(AttributeError):
        s.drift_ms = 999.0  # type: ignore[misc]


def test_age_s_with_injected_now():
    s = _sample(sampled_monotonic=100.0)
    assert loop_health.age_s(s, now=142.5) == 42.5


def test_age_s_clamps_negative_to_zero():
    """A monotonic reference that predates the sample must never yield a
    negative age (a negative age would read as 'fresher than fresh' and could
    mask a wedge)."""
    s = _sample(sampled_monotonic=100.0)
    assert loop_health.age_s(s, now=99.0) == 0.0


def test_age_s_zero_elapsed_is_zero():
    s = _sample(sampled_monotonic=100.0)
    assert loop_health.age_s(s, now=100.0) == 0.0


def test_age_s_defaults_to_monotonic_clock():
    """Without ``now=``, age is computed from time.monotonic() at read time and
    grows between reads (bounded sanity check, no sleeps)."""
    s = _sample(sampled_monotonic=time.monotonic())
    first = loop_health.age_s(s)
    second = loop_health.age_s(s)
    assert 0.0 <= first <= second < 60.0


# --- recent-lag memory (closes the cleared-republish TOCTOU race) ---


def test_recently_lagging_false_when_never_lagged():
    assert loop_health.recently_lagging(2.0) is False


def test_publish_lagging_records_recent_lag_within_window():
    loop_health.publish(_lagging_sample(sampled_monotonic=500.0))
    # lag observed at t=500; a read 1.0s later is within a 2.0s window, outside 0.5s
    assert loop_health.recently_lagging(2.0, now=501.0) is True
    assert loop_health.recently_lagging(0.5, now=501.0) is False


def test_publish_non_lagging_does_not_record_recent_lag():
    loop_health.publish(_sample())  # lagging=False
    assert loop_health.recently_lagging(9999.0, now=1e12) is False


def test_recent_lag_survives_cleared_republish():
    """The exact race: a lagging sample, then a cleared republish ~0.5s later.

    read() shows the cleared (lagging=False) sample, but recently_lagging still
    reports the just-observed lag — so a consumer reading just after the swap
    still sees live starvation evidence.
    """
    loop_health.publish(_lagging_sample(sampled_monotonic=500.0))
    cleared = LoopHealthSample(
        drift_ms=1.0,
        peak_ms=0.0,
        lagging=False,
        threshold_ms=250.0,
        executor=None,
        sampled_monotonic=500.5,
    )
    loop_health.publish(cleared)
    assert loop_health.read().lagging is False  # latest sample is the cleared one
    assert loop_health.recently_lagging(1.5, now=500.6) is True  # lag was 0.6s ago
