"""Tests for circuit breaker."""

from __future__ import annotations

from genesis.routing.circuit_breaker import (
    _MAX_OPEN_S,
    CircuitBreaker,
    CircuitBreakerRegistry,
)
from genesis.routing.types import (
    DegradationLevel,
    ErrorCategory,
    ProviderConfig,
    ProviderState,
)


def _provider(name: str = "test", ptype: str = "openai") -> ProviderConfig:
    return ProviderConfig(
        name=name,
        provider_type=ptype,
        model_id="m",
        is_free=False,
        rpm_limit=None,
        open_duration_s=120,
    )


def test_starts_closed():
    cb = CircuitBreaker(_provider())
    assert cb.state == ProviderState.CLOSED
    assert cb.is_available()


def test_consecutive_failures_trip():
    cb = CircuitBreaker(_provider(), failure_threshold=3, clock=lambda: 0)
    cb.record_failure(ErrorCategory.TRANSIENT)
    cb.record_failure(ErrorCategory.TRANSIENT)
    assert cb.state == ProviderState.CLOSED
    cb.record_failure(ErrorCategory.TRANSIENT)
    assert cb.state == ProviderState.OPEN
    assert not cb.is_available()


def test_open_to_half_open():
    t = [0.0]
    cb = CircuitBreaker(
        _provider(), failure_threshold=2, open_duration_s=10, clock=lambda: t[0]
    )
    cb.record_failure(ErrorCategory.TRANSIENT)
    cb.record_failure(ErrorCategory.TRANSIENT)
    assert cb.state == ProviderState.OPEN

    t[0] = 10.0
    assert cb.state == ProviderState.HALF_OPEN
    assert cb.is_available()


def test_half_open_to_closed():
    t = [0.0]
    cb = CircuitBreaker(
        _provider(),
        failure_threshold=2,
        open_duration_s=10,
        success_threshold=2,
        clock=lambda: t[0],
    )
    cb.record_failure(ErrorCategory.TRANSIENT)
    cb.record_failure(ErrorCategory.TRANSIENT)
    t[0] = 10.0
    assert cb.state == ProviderState.HALF_OPEN

    cb.record_success()
    assert cb.state == ProviderState.HALF_OPEN
    cb.record_success()
    assert cb.state == ProviderState.CLOSED


def test_half_open_to_open_on_failure():
    t = [0.0]
    cb = CircuitBreaker(
        _provider(), failure_threshold=2, open_duration_s=10, clock=lambda: t[0]
    )
    cb.record_failure(ErrorCategory.TRANSIENT)
    cb.record_failure(ErrorCategory.TRANSIENT)
    assert cb.trip_count == 1
    t[0] = 10.0
    assert cb.state == ProviderState.HALF_OPEN

    cb.record_failure(ErrorCategory.TRANSIENT)
    assert cb.state == ProviderState.OPEN
    assert cb.trip_count == 2  # re-trip from HALF_OPEN increments


def test_permanent_errors_trip():
    cb = CircuitBreaker(_provider(), failure_threshold=2, clock=lambda: 0)
    cb.record_failure(ErrorCategory.PERMANENT)
    cb.record_failure(ErrorCategory.PERMANENT)
    assert cb.state == ProviderState.OPEN


def test_success_resets_failure_count():
    cb = CircuitBreaker(_provider(), failure_threshold=3, clock=lambda: 0)
    cb.record_failure(ErrorCategory.TRANSIENT)
    cb.record_failure(ErrorCategory.TRANSIENT)
    cb.record_success()
    cb.record_failure(ErrorCategory.TRANSIENT)
    cb.record_failure(ErrorCategory.TRANSIENT)
    assert cb.state == ProviderState.CLOSED


def test_degraded_errors_trip():
    cb = CircuitBreaker(_provider(), failure_threshold=2, clock=lambda: 0)
    cb.record_failure(ErrorCategory.DEGRADED)
    cb.record_failure(ErrorCategory.DEGRADED)
    assert cb.state == ProviderState.OPEN


# --- probe_suspect tests ---


def test_probe_suspect_closed_to_half_open():
    """Probe suspect should move CLOSED → HALF_OPEN."""
    cb = CircuitBreaker(_provider())
    assert cb.state == ProviderState.CLOSED
    changed = cb.probe_suspect()
    assert changed is True
    assert cb.state == ProviderState.HALF_OPEN


def test_probe_suspect_noop_when_open():
    """Probe suspect should NOT change OPEN state (already worse)."""
    cb = CircuitBreaker(_provider(), failure_threshold=2, clock=lambda: 0)
    cb.record_failure(ErrorCategory.TRANSIENT)
    cb.record_failure(ErrorCategory.TRANSIENT)
    assert cb.state == ProviderState.OPEN
    changed = cb.probe_suspect()
    assert changed is False
    assert cb.state == ProviderState.OPEN


def test_probe_suspect_noop_when_half_open():
    """Probe suspect should NOT change HALF_OPEN (already suspect)."""
    t = [0.0]
    cb = CircuitBreaker(
        _provider(), failure_threshold=2, open_duration_s=10, clock=lambda: t[0]
    )
    cb.record_failure(ErrorCategory.TRANSIENT)
    cb.record_failure(ErrorCategory.TRANSIENT)
    t[0] = 10.0
    assert cb.state == ProviderState.HALF_OPEN
    changed = cb.probe_suspect()
    assert changed is False
    assert cb.state == ProviderState.HALF_OPEN


def test_probe_suspect_resets_consecutive_successes():
    """Probe suspect should clear success counter so recovery requires fresh successes."""
    cb = CircuitBreaker(_provider(), success_threshold=2)
    # Manually set to HALF_OPEN with 1 success already banked
    cb._state = ProviderState.HALF_OPEN
    cb._consecutive_successes = 1
    # Recover to CLOSED
    cb.record_success()
    assert cb.state == ProviderState.CLOSED
    # Now probe suspect — should reset successes
    changed = cb.probe_suspect()
    assert changed is True
    assert cb._consecutive_successes == 0


def test_probe_suspect_triggers_state_persistence():
    """Probe suspect should trigger on_state_change callback (state persistence)."""
    changes = []
    cb = CircuitBreaker(_provider(), on_state_change=lambda: changes.append(1))
    cb.probe_suspect()
    assert len(changes) == 1


# --- Escalating backoff tests ---


def test_trip_count_starts_zero():
    cb = CircuitBreaker(_provider())
    assert cb.trip_count == 0


def test_trip_count_increments_on_trip():
    cb = CircuitBreaker(_provider(), failure_threshold=2, clock=lambda: 0)
    cb.record_failure(ErrorCategory.TRANSIENT)
    cb.record_failure(ErrorCategory.TRANSIENT)
    assert cb.trip_count == 1
    assert cb.state == ProviderState.OPEN


def test_escalating_open_duration():
    """Open duration doubles with each trip: base=10 → 10, 20, 40, ..."""
    t = [0.0]
    cb = CircuitBreaker(
        _provider(), failure_threshold=2, open_duration_s=10, clock=lambda: t[0]
    )

    # Trip 1 (trip_count=1): effective = 10 * 2^max(0,1-1) = 10 * 2^0 = 10
    cb.record_failure(ErrorCategory.TRANSIENT)
    cb.record_failure(ErrorCategory.TRANSIENT)
    assert cb.trip_count == 1

    # Transitions at t=10 (base duration for first trip)
    t[0] = 10.0
    assert cb.state == ProviderState.HALF_OPEN

    # Trip 2 from HALF_OPEN (trip_count=2): effective = 10 * 2^max(0,2-1) = 10 * 2 = 20
    cb.record_failure(ErrorCategory.TRANSIENT)
    assert cb.trip_count == 2
    assert cb.state == ProviderState.OPEN

    # Still OPEN at t=20 (only 10s elapsed since trip at t=10, needs 20)
    t[0] = 20.0
    assert cb.state == ProviderState.OPEN

    # Transitions at t=30 (20s elapsed from trip at t=10)
    t[0] = 30.0
    assert cb.state == ProviderState.HALF_OPEN


def test_open_duration_caps_at_max():
    """Open duration should not exceed _MAX_OPEN_S (1800s)."""
    t = [0.0]
    cb = CircuitBreaker(
        _provider(), failure_threshold=1, open_duration_s=120, clock=lambda: t[0]
    )

    # Trip repeatedly to push trip_count high
    for _i in range(20):
        cb.record_failure(ErrorCategory.TRANSIENT)
        # Advance past the cap so HALF_OPEN triggers
        t[0] += _MAX_OPEN_S + 1
        assert cb.state == ProviderState.HALF_OPEN

    # At trip_count=20, uncapped would be 120 * 2^20 = ~125M seconds
    # Should be capped at _MAX_OPEN_S
    assert cb._effective_open_duration() == _MAX_OPEN_S


def test_trip_count_resets_on_recovery():
    """Trip count resets to 0 when breaker recovers HALF_OPEN → CLOSED."""
    t = [0.0]
    cb = CircuitBreaker(
        _provider(),
        failure_threshold=2,
        open_duration_s=10,
        success_threshold=2,
        clock=lambda: t[0],
    )

    # Trip 1 (trip_count=1): effective = 10 * 2^0 = 10
    cb.record_failure(ErrorCategory.TRANSIENT)
    cb.record_failure(ErrorCategory.TRANSIENT)
    assert cb.trip_count == 1

    t[0] = 10.0  # past effective open duration (10)
    assert cb.state == ProviderState.HALF_OPEN

    # Trip 2 from HALF_OPEN (trip_count=2): effective = 10 * 2^1 = 20
    cb.record_failure(ErrorCategory.TRANSIENT)
    assert cb.trip_count == 2

    t[0] = 30.0  # past effective open duration (10 + 20 = 30)
    assert cb.state == ProviderState.HALF_OPEN

    # Recover
    cb.record_success()
    cb.record_success()
    assert cb.state == ProviderState.CLOSED
    assert cb.trip_count == 0  # reset on recovery


# --- Registry tests ---

def test_registry_creates_breakers():
    providers = {"a": _provider("a"), "b": _provider("b")}
    reg = CircuitBreakerRegistry(providers, clock=lambda: 0)
    ba = reg.get("a")
    bb = reg.get("b")
    assert ba is not bb
    assert reg.get("a") is ba  # same instance


def test_degradation_l0():
    providers = {"a": _provider("a"), "b": _provider("b")}
    reg = CircuitBreakerRegistry(providers, clock=lambda: 0)
    assert reg.compute_degradation_level() == DegradationLevel.NORMAL


def test_degradation_l1():
    providers = {"a": _provider("a"), "b": _provider("b"), "c": _provider("c")}
    reg = CircuitBreakerRegistry(providers, clock=lambda: 0)
    # Trip one
    for _ in range(3):
        reg.get("a").record_failure(ErrorCategory.TRANSIENT)
    assert reg.compute_degradation_level() == DegradationLevel.FALLBACK


def test_degradation_l2():
    providers = {"a": _provider("a"), "b": _provider("b"), "c": _provider("c")}
    reg = CircuitBreakerRegistry(providers, clock=lambda: 0)
    for name in ["a", "b"]:
        for _ in range(3):
            reg.get(name).record_failure(ErrorCategory.TRANSIENT)
    assert reg.compute_degradation_level() == DegradationLevel.REDUCED


def test_degradation_l3_all_cloud_down():
    providers = {"a": _provider("a"), "b": _provider("b")}
    reg = CircuitBreakerRegistry(providers, clock=lambda: 0)
    for name in providers:
        for _ in range(3):
            reg.get(name).record_failure(ErrorCategory.TRANSIENT)
    assert reg.compute_degradation_level() == DegradationLevel.ESSENTIAL


def test_degradation_l5_all_ollama_down():
    providers = {
        "ol1": _provider("ol1", "ollama"),
        "ol2": _provider("ol2", "ollama"),
    }
    reg = CircuitBreakerRegistry(providers, clock=lambda: 0)
    for name in providers:
        for _ in range(3):
            reg.get(name).record_failure(ErrorCategory.TRANSIENT)
    assert reg.compute_degradation_level() == DegradationLevel.LOCAL_COMPUTE_DOWN


# --- Save/load round-trip tests ---


def test_trip_count_survives_save_load(tmp_path):
    """Trip count should persist through save/load cycle."""
    import genesis.routing.circuit_breaker as cb_mod

    original_path = cb_mod._STATE_FILE
    cb_mod._STATE_FILE = tmp_path / "cb_state.json"
    try:
        providers = {"x": _provider("x")}
        reg = CircuitBreakerRegistry(providers, clock=lambda: 0)

        # Trip twice to get trip_count=2
        cb = reg.get("x")
        for _ in range(3):
            cb.record_failure(ErrorCategory.TRANSIENT)
        assert cb.trip_count == 1
        # Advance to HALF_OPEN, then re-trip
        cb._state = ProviderState.HALF_OPEN
        cb.record_failure(ErrorCategory.TRANSIENT)
        assert cb.trip_count == 2

        reg.save_state()

        # Create new registry, load state
        reg2 = CircuitBreakerRegistry(providers, clock=lambda: 0)
        cb2 = reg2.get("x")
        assert cb2._trip_count == 2
    finally:
        cb_mod._STATE_FILE = original_path
