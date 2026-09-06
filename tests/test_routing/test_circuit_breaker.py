"""Tests for circuit breaker."""

from __future__ import annotations

import json
import logging

from genesis.routing.circuit_breaker import (
    _LONG_OPEN_CATEGORIES,
    _MAX_OPEN_S,
    _MAX_QUOTA_OPEN_S,
    CircuitBreaker,
    CircuitBreakerRegistry,
)
from genesis.routing.types import (
    DegradationLevel,
    ErrorCategory,
    ProviderConfig,
    ProviderState,
)


def _provider(name: str = "test", ptype: str = "openai", is_free: bool = False) -> ProviderConfig:
    return ProviderConfig(
        name=name,
        provider_type=ptype,
        model_id="m",
        is_free=is_free,
        rpm_limit=None,
        open_duration_s=120,
    )


def test_starts_closed():
    cb = CircuitBreaker(_provider())
    assert cb.state == ProviderState.CLOSED
    assert cb.is_available()


def test_registry_persists_breaker_state_on_trip(tmp_path):
    """Default registry writes breaker state to disk when a breaker trips."""
    state_file = tmp_path / "cb_state.json"
    reg = CircuitBreakerRegistry({"p": _provider("p")}, state_file=state_file)
    cb = reg.get("p")
    for _ in range(3):  # default failure_threshold trips on the 3rd
        cb.record_failure(ErrorCategory.TRANSIENT)
    assert state_file.exists()
    data = json.loads(state_file.read_text())
    assert data["p"]["state"] == "open"
    # The atomic tmp+rename must leave no stray temp files.
    assert list(state_file.parent.glob("*.tmp")) == []


def test_standalone_registry_is_read_only(tmp_path):
    """A persist=False registry (MCP children) must never write the shared state file."""
    state_file = tmp_path / "cb_state.json"
    reg = CircuitBreakerRegistry({"p": _provider("p")}, state_file=state_file, persist=False)
    cb = reg.get("p")
    for _ in range(5):
        cb.record_failure(ErrorCategory.TRANSIENT)
    assert cb.state == ProviderState.OPEN  # breaker still works in-memory
    assert not state_file.exists()  # but nothing was written to the shared file


def test_read_only_registry_still_loads_existing_state(tmp_path):
    """persist=False must still LOAD server-written state at construction."""
    state_file = tmp_path / "cb_state.json"
    server = CircuitBreakerRegistry({"p": _provider("p")}, state_file=state_file)
    sb = server.get("p")
    for _ in range(3):
        sb.record_failure(ErrorCategory.TRANSIENT)
    assert state_file.exists()

    child = CircuitBreakerRegistry(
        {"p": _provider("p")}, state_file=state_file, persist=False
    )
    assert child.get("p").state == ProviderState.OPEN


def test_load_state_restores_open_after_restart(tmp_path):
    """Regression: a persisted OPEN breaker must reload as OPEN.

    save_state writes ProviderState.OPEN.value ('open'); load_state previously
    compared against the literal 'OPEN', so a tripped provider silently came back
    CLOSED on every restart.
    """
    state_file = tmp_path / "cb_state.json"
    state_file.write_text(
        json.dumps(
            {
                "p": {
                    "state": "open",
                    "consecutive_failures": 0,
                    "trip_count": 1,
                    "last_failure_category": "transient",
                }
            }
        )
    )
    reg = CircuitBreakerRegistry({"p": _provider("p")}, state_file=state_file)
    assert reg.get("p").state == ProviderState.OPEN


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


def test_legacy_degradation_path_warns_once(caplog):
    """Entering the count-based legacy fallback (no essential map injected)
    surfaces a one-time warning so a misconfigured install is not silently
    mis-degrading — but the warning does not repeat on the same instance."""
    providers = {"a": _provider("a"), "b": _provider("b")}
    reg = CircuitBreakerRegistry(providers, clock=lambda: 0)
    with caplog.at_level(logging.WARNING, logger="genesis.routing.circuit_breaker"):
        reg.compute_degradation_level()
        reg.compute_degradation_level()  # second call must NOT re-warn
    hits = [r for r in caplog.records if "LEGACY provider-count fallback" in r.message]
    assert len(hits) == 1


def test_coverage_degradation_path_does_not_warn(caplog):
    """With an essential-site map injected, the coverage path is used and the
    legacy-fallback warning never fires."""
    providers = {
        "p1": _provider("p1", "openrouter"),
        "p2": _provider("p2", "google", is_free=True),
    }
    essential = {"9_fact_extraction": ["p1", "p2"]}
    reg = CircuitBreakerRegistry(providers, clock=lambda: 0, essential_sites=essential)
    with caplog.at_level(logging.WARNING, logger="genesis.routing.circuit_breaker"):
        reg.compute_degradation_level()
    assert not [r for r in caplog.records if "LEGACY provider-count fallback" in r.message]


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


def test_free_provider_down_does_not_degrade():
    """Free-tier providers being OPEN should not affect degradation level."""
    providers = {
        "paid1": _provider("paid1"),
        "paid2": _provider("paid2"),
        "free1": _provider("free1", is_free=True),
    }
    reg = CircuitBreakerRegistry(providers, clock=lambda: 0)
    # Trip the free provider
    for _ in range(3):
        reg.get("free1").record_failure(ErrorCategory.TRANSIENT)
    assert reg.get("free1").state == ProviderState.OPEN
    # Degradation should still be L0 — free providers don't count
    assert reg.compute_degradation_level() == DegradationLevel.NORMAL


def test_trip_count_capped_on_restore(tmp_path):
    """Trip count should be capped to 3 when restoring OPEN state."""
    import json

    import genesis.routing.circuit_breaker as cb_mod

    original_path = cb_mod._STATE_FILE
    cb_mod._STATE_FILE = tmp_path / "cb_state.json"
    try:
        # Write state with high trip_count (simulating weeks of restarts).
        # Note: the persisted value is the StrEnum value "open" (what save_state
        # writes) — the literal "OPEN" never appears on disk.
        state = {
            "x": {
                "state": "open",
                "trip_count": 90,
                "consecutive_failures": 5,
            }
        }
        (tmp_path / "cb_state.json").write_text(json.dumps(state))

        providers = {"x": _provider("x")}
        reg = CircuitBreakerRegistry(providers, clock=lambda: 0)
        cb = reg.get("x")
        assert cb._state == ProviderState.OPEN
        assert cb._trip_count == 3  # capped from 90
    finally:
        cb_mod._STATE_FILE = original_path


# --- Probe-based recovery (record_probe_success) ---


def _half_open_breaker(probe_threshold=3, **kw):
    """A breaker driven to HALF_OPEN: tripped OPEN (failure_threshold=2), then
    its open window expired so the next state read auto-transitions to HALF_OPEN.
    """
    t = [0.0]
    cb = CircuitBreaker(
        _provider("p1"), failure_threshold=2, open_duration_s=10,
        clock=lambda: t[0], probe_success_threshold=probe_threshold, **kw,
    )
    cb.record_failure(ErrorCategory.TRANSIENT)
    cb.record_failure(ErrorCategory.TRANSIENT)  # trips OPEN
    assert cb.state == ProviderState.OPEN
    t[0] = 100.0  # past the open window
    assert cb.state == ProviderState.HALF_OPEN
    return cb, t


def _probe_suspected_breaker(probe_threshold=3, **kw):
    """A breaker in HALF_OPEN because a PROBE suspected it — no call failed, so
    it never tripped and `_opened_by_call` is False. This is the only state a
    clean probe may clear under evidence symmetry."""
    cb = CircuitBreaker(
        _provider("p1"), failure_threshold=2, open_duration_s=10,
        clock=lambda: 0.0, probe_success_threshold=probe_threshold, **kw,
    )
    assert cb.probe_suspect() is True
    assert cb.state == ProviderState.HALF_OPEN
    assert cb._opened_by_call is False
    return cb


def test_probe_success_heals_a_probe_suspected_breaker_after_threshold():
    """CHANGED from `test_probe_success_heals_half_open_after_threshold`.

    That test drove HALF_OPEN with real call failures and expected a probe to
    close it — PR #705's behaviour. Under evidence symmetry a probe may only
    undo a probe-caused suspicion; the call-tripped case is covered by
    `test_a_call_tripped_breaker_is_not_probe_healed`.
    """
    cb = _probe_suspected_breaker(probe_threshold=3)
    cb.record_probe_success()
    assert cb.state == ProviderState.HALF_OPEN  # 1 < 3
    cb.record_probe_success()
    assert cb.state == ProviderState.HALF_OPEN  # 2 < 3
    cb.record_probe_success()
    assert cb.state == ProviderState.CLOSED     # 3 → healed
    assert cb.trip_count == 0


def test_a_call_tripped_breaker_is_not_probe_healed():
    """The inverse, and the whole point: real failures need a real success."""
    cb, _ = _half_open_breaker(probe_threshold=3)
    assert cb._opened_by_call is True
    for _ in range(10):
        cb.record_probe_success()
    assert cb.state == ProviderState.HALF_OPEN, (
        "a listing probe closed a breaker that real calls opened"
    )
    cb.record_success()
    cb.record_success()  # success_threshold=2
    assert cb.state == ProviderState.CLOSED
    assert cb._opened_by_call is False


def test_probe_success_noop_when_closed():
    cb = CircuitBreaker(_provider(), clock=lambda: 0)
    assert cb.state == ProviderState.CLOSED
    cb.record_probe_success()
    assert cb.state == ProviderState.CLOSED
    assert cb.trip_count == 0


def test_probe_success_noop_when_open_not_expired():
    """A probe success must NOT heal a breaker whose open window has not yet
    expired (still genuinely OPEN)."""
    t = [0.0]
    cb = CircuitBreaker(_provider(), failure_threshold=2, open_duration_s=100, clock=lambda: t[0])
    cb.record_failure(ErrorCategory.TRANSIENT)
    cb.record_failure(ErrorCategory.TRANSIENT)
    assert cb.state == ProviderState.OPEN
    cb.record_probe_success()
    assert cb.state == ProviderState.OPEN


def test_probe_success_never_fires_on_recovery():
    """INVERTED from `test_probe_success_fires_on_recovery` (PR #705).

    #705 wired `on_recovery` into the probe path deliberately, so "a probe-healed
    provider does not leave a stale 'provider_failure' observation". That hook
    resolves the observation and clears `first_trip_at` — the only per-provider
    "failing since" timestamp — and it fired on evidence (a 200 from
    /v1/models) that says nothing about whether calls work. Measured over the
    outage that began 2026-08-27: eight separate observations, ALL of them
    auto-resolved that way while the provider returned zero successes, so no
    surface could report the real duration.

    The call is now GONE from the probe path rather than guarded. Reaching a
    probe heal means the breaker was probe-suspected, which never trips and so
    never escalates — there is no observation to resolve. `record_success`
    still fires it, which is the correct and only path.
    """
    recovered = []
    cb = CircuitBreaker(
        _provider("p1"), failure_threshold=2, open_duration_s=1000,
        clock=lambda: 0.0, probe_success_threshold=2,
        on_recovery=lambda n: recovered.append(n),
    )
    # CLOSED but still carrying a trip count — a real success while the open
    # window has not expired. `was_tripped` reads trip_count, so before this
    # change the probe heal below DID fire on_recovery from here.
    #
    # DEFENSIVE: both production callers guard on `is_available()`, so nothing
    # reaches this state today. Exercised directly because it is the only state
    # that tells the two close paths apart.
    cb.record_failure(ErrorCategory.TRANSIENT)
    cb.record_failure(ErrorCategory.TRANSIENT)
    cb.record_success()
    assert cb.state == ProviderState.CLOSED
    assert cb.trip_count > 0

    cb.probe_suspect()
    cb.record_probe_success()
    cb.record_probe_success()
    assert cb.state == ProviderState.CLOSED
    assert recovered == [], "a probe fired on_recovery and could erase an outage record"


def test_real_success_still_fires_on_recovery():
    """The path that SHOULD resolve the observation is untouched."""
    recovered = []
    t = [0.0]
    cb = CircuitBreaker(
        _provider("p1"), failure_threshold=2, open_duration_s=10,
        clock=lambda: t[0], on_recovery=lambda name: recovered.append(name),
    )
    cb.record_failure(ErrorCategory.TRANSIENT)
    cb.record_failure(ErrorCategory.TRANSIENT)
    t[0] = 100.0
    assert cb.state == ProviderState.HALF_OPEN
    cb.record_success()
    assert recovered == []  # below success_threshold
    cb.record_success()
    assert cb.state == ProviderState.CLOSED
    assert recovered == ["p1"]


def test_real_failure_after_probe_heal_can_retrip():
    """A probe-healed provider must still re-trip on real failures — healing
    must not disable failure tracking. Driven from a probe-caused suspicion now,
    since that is the only heal a probe can perform."""
    cb = _probe_suspected_breaker(probe_threshold=2)
    cb.record_probe_success()
    cb.record_probe_success()
    assert cb.state == ProviderState.CLOSED
    cb.record_failure(ErrorCategory.TRANSIENT)
    cb.record_failure(ErrorCategory.TRANSIENT)  # failure_threshold=2
    assert cb.state == ProviderState.OPEN
    assert cb._opened_by_call is True


# --- Coverage-based degradation (essential_sites map injected) ---


def test_degradation_coverage_paid_down_but_essentials_covered_is_normal():
    """The demo scenario: the PAID provider is down (e.g. OpenRouter out of
    credits), but every essential cloud site still has a free provider up →
    coverage-based degradation = NORMAL. No false 'all cloud down ⇒ ESSENTIAL'.
    """
    providers = {
        "paid_or": _provider("paid_or", "openrouter"),
        "free_g": _provider("free_g", "google", is_free=True),
        "free_q": _provider("free_q", "groq", is_free=True),
    }
    essential = {
        "4_light_reflection": ["paid_or", "free_g", "free_q"],
        "3_micro_reflection": ["paid_or", "free_q"],
    }
    reg = CircuitBreakerRegistry(providers, clock=lambda: 0, essential_sites=essential)
    for _ in range(3):
        reg.get("paid_or").record_failure(ErrorCategory.QUOTA_EXHAUSTED)
    assert reg.get("paid_or").state == ProviderState.OPEN
    assert reg.compute_degradation_level() == DegradationLevel.NORMAL


def test_degradation_coverage_essential_uncovered_is_essential():
    """When an essential site has NO available provider, degrade to ESSENTIAL."""
    providers = {
        "p1": _provider("p1", "openrouter"),
        "p2": _provider("p2", "google", is_free=True),
    }
    essential = {"9_fact_extraction": ["p1", "p2"]}
    reg = CircuitBreakerRegistry(providers, clock=lambda: 0, essential_sites=essential)
    for name in ("p1", "p2"):
        for _ in range(3):
            reg.get(name).record_failure(ErrorCategory.TRANSIENT)
    assert reg.compute_degradation_level() == DegradationLevel.ESSENTIAL


def test_degradation_coverage_missing_api_key_uncovers_site():
    """A provider with no API key cannot cover an essential site even when its
    breaker is CLOSED."""
    providers = {
        "nokey": ProviderConfig(
            name="nokey", provider_type="openrouter", model_id="m",
            is_free=False, rpm_limit=None, open_duration_s=120,
            has_api_key=False,
        ),
    }
    essential = {"40_ego_focus_selection": ["nokey"]}
    reg = CircuitBreakerRegistry(providers, clock=lambda: 0, essential_sites=essential)
    assert reg.get("nokey").is_available()  # breaker closed...
    assert reg.compute_degradation_level() == DegradationLevel.ESSENTIAL  # ...but no key


def test_degradation_coverage_all_healthy_is_normal():
    """All essentials covered, nothing down → NORMAL."""
    providers = {
        "p1": _provider("p1", "openrouter"),
        "p2": _provider("p2", "google", is_free=True),
    }
    essential = {"8_ego_compaction": ["p1", "p2"]}
    reg = CircuitBreakerRegistry(providers, clock=lambda: 0, essential_sites=essential)
    assert reg.compute_degradation_level() == DegradationLevel.NORMAL


def test_degradation_coverage_unknown_provider_in_chain_is_unavailable():
    """A provider name in an essential chain that isn't registered counts as
    unavailable — if it's the only one, the site is uncovered."""
    providers = {"real": _provider("real", "google", is_free=True)}
    essential = {"3_micro_reflection": ["ghost"]}  # 'ghost' not in providers
    reg = CircuitBreakerRegistry(providers, clock=lambda: 0, essential_sites=essential)
    assert reg.compute_degradation_level() == DegradationLevel.ESSENTIAL


def test_degradation_coverage_ollama_axis_independent_of_essential_map():
    """Ollama (local-compute) axis is checked before cloud coverage, even when
    the essential map is present."""
    providers = {
        "ol": _provider("ol", "ollama"),
        "free_g": _provider("free_g", "google", is_free=True),
    }
    essential = {"4_light_reflection": ["free_g"]}
    reg = CircuitBreakerRegistry(providers, clock=lambda: 0, essential_sites=essential)
    for _ in range(3):
        reg.get("ol").record_failure(ErrorCategory.TRANSIENT)
    assert reg.compute_degradation_level() == DegradationLevel.LOCAL_COMPUTE_DOWN


def test_chain_has_available_true_when_any_provider_closed():
    reg = CircuitBreakerRegistry({"a": _provider("a"), "b": _provider("b")})
    for _ in range(3):  # trip 'a' OPEN (default threshold trips on the 3rd)
        reg.get("a").record_failure(ErrorCategory.TRANSIENT)
    assert reg.get("a").state == ProviderState.OPEN
    assert reg.chain_has_available(["a", "b"]) is True  # 'b' still CLOSED


def test_chain_has_available_false_when_all_open():
    reg = CircuitBreakerRegistry({"a": _provider("a"), "b": _provider("b")})
    for name in ("a", "b"):
        for _ in range(3):
            reg.get(name).record_failure(ErrorCategory.TRANSIENT)
    assert reg.chain_has_available(["a", "b"]) is False


def test_chain_has_available_false_for_empty_or_unknown():
    reg = CircuitBreakerRegistry({"a": _provider("a")})
    assert reg.chain_has_available([]) is False
    assert reg.chain_has_available(["nonexistent"]) is False


# --- Origin flag: schema migration and operator transitions ---
#
# `_opened_by_call` is a NEW persisted field. Two ways to get it wrong, both
# found by review rather than by me, and both of the same shape: something
# that changes breaker state without maintaining the flag. The migration below
# covers the file written by a version that predates the field; the operator
# transitions cover the only external mutation site in the tree.


def _write_state(tmp_path, rows):
    import json

    (tmp_path / "cb_state.json").write_text(json.dumps(rows))


def _restore(tmp_path, rows, name="x"):
    """Load `rows` through the real load_state and return the breaker."""
    import genesis.routing.circuit_breaker as cb_mod

    original = cb_mod._STATE_FILE
    cb_mod._STATE_FILE = tmp_path / "cb_state.json"
    try:
        _write_state(tmp_path, rows)
        reg = CircuitBreakerRegistry({name: _provider(name)}, clock=lambda: 0)
        return reg.get(name)
    finally:
        cb_mod._STATE_FILE = original


def test_a_legacy_open_row_without_the_origin_flag_restores_as_call_opened(tmp_path):
    """A state file written before `opened_by_call` existed has no such key,
    and reading that absence as False is the one origin the row cannot have.

    In every version that wrote a keyless file, `probe_suspect()` produced
    HALF_OPEN and never OPEN, and a non-OPEN save restored as CLOSED. So a
    persisted OPEN can ONLY have come from a real call trip or an operator
    disable — both of which must refuse probe healing.

    Not hypothetical: MEASURED against this deploy's own live state file, the
    provider in the motivating outage is persisted OPEN with no key. Defaulting
    to False would have left the very breaker this branch exists to protect
    probe-healable for one more cycle, so the fix would have failed its own
    acceptance bar on the incident that motivated it.
    """
    cb = _restore(tmp_path, {"x": {"state": "open", "trip_count": 2}})
    assert cb._state == ProviderState.OPEN
    assert cb._opened_by_call is True, (
        "a legacy OPEN row has no origin recorded; it must default to "
        "call-opened, because a probe could never have opened it"
    )


def test_a_legacy_row_saved_closed_never_gains_the_origin_flag(tmp_path):
    """The migration default must not leak onto a breaker that is not OPEN.

    Boundary in the other direction: `_opened_by_call` qualifies an OPEN
    breaker. Defaulting a CLOSED row to True would be the poison pill in the
    new field — a healthy provider that refuses probe healing forever after
    the next blip. Pins the `saved_state == open` condition, not just the
    default value.
    """
    cb = _restore(tmp_path, {"x": {"state": "closed", "trip_count": 0}})
    assert cb._state == ProviderState.CLOSED
    assert cb._opened_by_call is False


def test_an_explicit_probe_origin_on_an_open_row_survives_the_migration(tmp_path):
    """A NEW-format file states the origin; the default must not overwrite it.

    Precisely what this pins, since the obvious wording overstates it: no
    `save_state` can currently produce `{"state": "open", "opened_by_call":
    false}` -- every path that sets OPEN also sets the flag True -- so this is
    NOT protecting existing data. It pins the migration's SHAPE: a default must
    fill an absent key, never override a present one. Written as an
    unconditional True it would pass every other test here and silently
    misreport any future OPEN-with-probe-origin state.
    """
    cb = _restore(
        tmp_path,
        {"x": {"state": "open", "trip_count": 1, "opened_by_call": False}},
    )
    assert cb._state == ProviderState.OPEN
    assert cb._opened_by_call is False


def test_operator_disable_refuses_probe_healing():
    """`force_open()` records the disable as call-origin, so a listing probe
    cannot quietly undo an explicit human decision.

    Evidence symmetry says a probe may only clear a probe's own suspicion. An
    operator taking a provider out of rotation is not one — it is at least as
    strong a statement as a failed call. Before the chokepoint the dashboard
    assigned the private fields directly and left the flag False, so three
    clean probes closed the breaker the user had just disabled.
    """
    t = [0.0]
    cb = CircuitBreaker(
        _provider("p1"), failure_threshold=2, open_duration_s=10,
        clock=lambda: t[0], probe_success_threshold=3,
    )
    cb.force_open()
    assert cb._state == ProviderState.OPEN
    assert cb._trip_count == 99
    assert cb._opened_by_call is True

    t[0] = 10_000.0  # past any window; .state auto-transitions to HALF_OPEN
    assert cb.state == ProviderState.HALF_OPEN
    for _ in range(5):
        cb.record_probe_success()
    assert cb.state == ProviderState.HALF_OPEN, (
        "probes must not close a breaker a human disabled on purpose"
    )
    assert cb._trip_count == 99, "a probe must not reset the operator's hold"


def test_operator_re_enable_clears_the_origin_flag():
    """`force_close()` must clear `_opened_by_call` along with the counters.

    Leaving it True puts "a real call opened this" on a breaker a human has
    just declared healthy. The next probe blip moves it to HALF_OPEN, every
    clean probe is then refused, and the dashboard reads "unverified" until
    real traffic or a restart — in defiance of the explicit reset.
    """
    cb = CircuitBreaker(
        _provider("p1"), failure_threshold=2, open_duration_s=10,
        clock=lambda: 0.0, probe_success_threshold=3,
    )
    cb.record_failure(ErrorCategory.PERMANENT)
    cb.record_failure(ErrorCategory.PERMANENT)  # trips OPEN, flag True
    assert cb._opened_by_call is True

    cb.force_close()
    assert cb._state == ProviderState.CLOSED
    assert cb._trip_count == 0
    assert cb._opened_by_call is False

    # A later probe blip must now be clearable, exactly as for any healthy
    # provider — this is what the stale flag would have prevented.
    assert cb.probe_suspect() is True
    for _ in range(3):
        cb.record_probe_success()
    assert cb.state == ProviderState.CLOSED


def test_operator_transitions_persist_through_the_change_hook_alone():
    """The dashboard route no longer calls `save_state()` itself, so the
    operator's decision reaches disk ONLY via `_notify_change`.

    That is the point of making these complete transitions -- a caller should
    not have to remember to persist -- but it turns a belt-and-braces double
    write into a single dependency, so it gets a test rather than an
    assumption. Without this, unwiring `on_state_change` would silently mean a
    provider a human disabled comes back on the next restart, with nothing
    failing anywhere.
    """
    import json
    import pathlib
    import tempfile

    d = pathlib.Path(tempfile.mkdtemp())
    state_file = d / "cb.json"
    reg = CircuitBreakerRegistry(
        {"p1": _provider("p1")}, state_file=state_file, persist=True
    )
    cb = reg.get("p1")
    assert not state_file.exists()

    cb.force_open()  # no explicit save_state anywhere in this test
    assert state_file.exists(), "operator disable never reached disk"
    row = json.loads(state_file.read_text())["p1"]
    assert row["state"] == "open"
    assert row["trip_count"] == 99
    assert row["opened_by_call"] is True

    cb.force_close()
    row = json.loads(state_file.read_text())["p1"]
    assert row["state"] == "closed"
    assert row["opened_by_call"] is False
    assert row["trip_count"] == 0


def test_no_module_outside_the_breaker_assigns_its_private_state():
    """LOCK: breaker state may only be changed by the breaker's own methods.

    Two review findings on this branch had ONE shape between them -- something
    changed breaker state without maintaining `_opened_by_call`. That is a
    CONVENTION: every external mutation site has to remember a rule, and the
    only one that existed did not. `force_open()`/`force_close()` move the
    obligation into the class; this test stops a new caller reintroducing it.

    This is an AST check, not a regex, because two successive audits found
    defects in the regex versions rather than in the rule -- first a receiver
    blind spot (it matched `cb.` but not `breakers.get(name).`), then an
    over-match onto unrelated classes' own `self._state`, plus comment,
    docstring and keyword-argument false positives. Parsing removes that whole
    class: a comment is not an assignment, a keyword argument is not an
    attribute, and the receiver is a node rather than a name to be spelled.

    The rule enforced is precise: **assigning a guarded private attribute on
    some OTHER object**. A class assigning its own `self._state` is its own
    business -- several unrelated state machines here do exactly that -- so
    `self`/`cls` receivers are excluded by construction rather than by hoping
    a file filter never admits them.

    HONEST SCOPE: it sees `.py` under `src/genesis` and `scripts/`. It cannot
    see a mutation routed through `__dict__`, `object.__setattr__`, or a
    dynamically built attribute name.
    """
    import ast
    import pathlib

    roots = [
        pathlib.Path(__file__).resolve().parents[2] / "src" / "genesis",
        pathlib.Path(__file__).resolve().parents[2] / "scripts",
    ]
    owner = roots[0] / "routing" / "circuit_breaker.py"
    assert owner.is_file(), f"guard is blind: {owner} not found"

    FIELDS = {
        "_state", "_opened_by_call", "_opened_at", "_trip_count",
        "_consecutive_failures", "_consecutive_successes",
        "_last_failure_category",
    }

    def _receiver_is_self(node):
        return isinstance(node, ast.Name) and node.id in ("self", "cls")

    def offenders_in(tree):
        out = []
        for node in ast.walk(tree):
            targets = []
            if isinstance(node, ast.Assign):
                targets = list(node.targets)
            elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
                targets = [node.target]
            for t in targets:
                for sub in ([*t.elts] if isinstance(t, ast.Tuple) else [t]):
                    if (isinstance(sub, ast.Attribute) and sub.attr in FIELDS
                            and not _receiver_is_self(sub.value)):
                        out.append((sub.lineno, sub.attr))
            # setattr(x, "_state", ...) on a non-self receiver
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "setattr" and len(node.args) >= 2
                    and isinstance(node.args[1], ast.Constant)
                    and node.args[1].value in FIELDS
                    and not _receiver_is_self(node.args[0])):
                out.append((node.lineno, f"setattr {node.args[1].value}"))
        return out

    # ANCHOR — shapes the guard MUST catch. Replaces a file-count check, which
    # only proved files were opened, not that the check still fires.
    must_catch = [
        "cb._state = 1",
        "breakers.get(name)._state = 1",
        "provider_breaker._trip_count = 0",
        "cb._trip_count += 1",
        "cbs[name]._opened_by_call = True",
        "cb._state, cb._trip_count = 1, 99",
        'setattr(cb, "_state", 1)',
    ]
    for src in must_catch:
        assert offenders_in(ast.parse(src)), f"guard has gone blind on: {src}"
    # ...and shapes it must NOT catch, incl. an unrelated class's own state and
    # the prose/kwarg forms that defeated the regex versions.
    must_ignore = [
        "self._state = NetworkStatus.OFFLINE",
        "self._consecutive_failures = 0",
        "foo(_state=1)",
        "_state = compute()",
        "if cb.state == 1: pass",
        "assert cb._trip_count == 0",
        "opened = cb._opened_by_call",
        '"""docs: cb._state = ProviderState.OPEN is forbidden."""',
    ]
    for src in must_ignore:
        assert not offenders_in(ast.parse(src)), f"guard over-matches: {src}"

    offenders, scanned = [], set()
    for root in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.py")):
            if path == owner:
                continue
            try:
                tree = ast.parse(path.read_text())
            except SyntaxError:
                continue
            rel = str(path.relative_to(root.parent))
            scanned.add(rel)
            for lineno, attr in offenders_in(tree):
                offenders.append(f"{rel}:{lineno}: assigns {attr}")

    # Pin the files that actually hold breaker objects, so a refactor cannot
    # quietly drop one out of scope (the failure mode of the previous version,
    # whose substring filter excluded routing.py entirely).
    must_scan = {
        "genesis/dashboard/routes/providers.py",
        "genesis/dashboard/routes/routing.py",
        "genesis/observability/snapshots/api_keys.py",
        "genesis/observability/provider_health.py",
        "genesis/routing/router.py",
        "genesis/dashboard/routes/vitals.py",
    }
    missing = must_scan - scanned
    assert not missing, f"guard went blind on breaker-handling files: {missing}"

    assert not offenders, (
        "these modules change circuit-breaker private state on another object. "
        "Use the breaker's own transitions (force_open / force_close) so a new "
        "persisted field cannot be silently forgotten:\n  "
        + "\n  ".join(offenders)
    )


def test_a_half_open_row_keeps_its_state_and_origin_across_a_restart(tmp_path):
    """The guarantee must survive the restart that routinely follows a trip.

    REGRESSION (#1563, found by the GLM cross-model reviewer after Codex and two
    internal audits missed it). The path is ordinary, not adversarial:

      1. a dead provider call-trips -> persisted {"state":"open",
         "opened_by_call":true};
      2. its backoff window expires;
      3. ANY read of `.state` — the health snapshot, the routing config route,
         the vitals route — silently assigns HALF_OPEN, because the property
         mutates on read and never calls `_notify_change`;
      4. the next state change of ANY OTHER breaker calls `save_state`, which
         serialises every breaker's raw `_state`, persisting this one as
         "half_open";
      5. a restart then restored it CLOSED with `opened_by_call=False`.

    So after every merge-restart the dead provider read HEALTHY and was
    probe-healable again — the original bug this PR exists to remove, returning
    on a timer. Restoring HALF_OPEN with its origin was dropped from the plan on
    the stated rationale that "a breaker restored CLOSED cannot be wrongly
    probe-healed"; that is true and beside the point, because the harm is the
    breaker reading CLOSED for a provider that is dead.
    """
    cb = _restore(
        tmp_path,
        {"x": {"state": "half_open", "trip_count": 2, "opened_by_call": True}},
    )
    assert cb._state == ProviderState.HALF_OPEN, (
        "a half_open row restored as CLOSED — the provider reads healthy after "
        "a restart though no real call ever succeeded"
    )
    assert cb._opened_by_call is True, (
        "the origin flag was dropped on restore, so a listing probe can heal a "
        "breaker that real calls opened"
    )


def test_a_restart_keeps_the_failure_reason_for_a_half_open_row(tmp_path):
    """The failure category is a live fact about a breaker that is still not closed.

    REGRESSION (#1563). Blanking it for every non-OPEN row stripped the reason
    from two production readers — `observability/snapshots/api_keys.py` renders
    it as `reason`, and `dashboard/routes/vitals.py` as `last_failure` — so a
    provider persisted half_open came back with no explanation until it failed
    again. On `origin/main` the restore was unconditional; narrowing it to OPEN
    was a diagnostic regression introduced by this branch.
    """
    cb = _restore(
        tmp_path,
        {
            "x": {
                "state": "half_open",
                "trip_count": 2,
                "opened_by_call": True,
                "last_failure_category": "quota_exhausted",
            }
        },
    )
    assert cb._last_failure_category == ErrorCategory.QUOTA_EXHAUSTED, (
        "the failure reason was blanked on restart; the dashboard and the API-key "
        "snapshot lose why this provider is not closed"
    )


# --- open-duration cap by failure category ---

class TestOpenDurationCapByCategory:
    """Which categories hold the breaker open on the LONG (4h) cap.

    Previously untested. Partitioning the enum is NOT enough on its own —
    see `test_every_member_has_an_explicit_cap_decision` for why, and for
    the guard that actually catches an undecided new member.
    """

    def _tripped_cap(self, category: ErrorCategory) -> float:
        # Trip well past the threshold so escalating backoff saturates and the
        # cap — not the doubling — is what the duration reports.
        cb = CircuitBreaker(_provider(), failure_threshold=1, open_duration_s=120)
        for _ in range(12):
            cb.record_failure(category)
        return cb._effective_open_duration()

    def test_every_member_has_an_explicit_cap_decision(self):
        # The guard the other two tests CANNOT provide: they partition the enum
        # into the long-cap set and "everything else", so a new member added to
        # neither set lands in the short-cap branch and SATISFIES the assertion
        # — the skip branch is the default branch. Only pinning the whole enum
        # turns "nobody decided" into a failure.
        assert {c.value for c in ErrorCategory} == {
            "transient", "degraded", "permanent", "quota_exhausted",
            "timeout", "rate_limited", "bad_request", "not_entitled",
        }, "new ErrorCategory member — decide whether it belongs in _LONG_OPEN_CATEGORIES"

    def test_long_cap_categories(self):
        # Pin MEMBERSHIP first. The loop below reads the production set so the
        # literal and the frozenset cannot drift apart — but that alone makes
        # the pair of cap tests a PARTITION of set(ErrorCategory), and every
        # partition passes: moving TIMEOUT into the long set would leave this
        # test and test_every_other_category_gets_the_short_cap both green
        # while a transient error held the 4h cap.
        assert _LONG_OPEN_CATEGORIES == {
            ErrorCategory.QUOTA_EXHAUSTED,
            ErrorCategory.NOT_ENTITLED,
        }, "membership changed — a category gained or lost the 4h cap"
        for category in _LONG_OPEN_CATEGORIES:
            assert self._tripped_cap(category) == _MAX_QUOTA_OPEN_S, category

    def test_every_other_category_gets_the_short_cap(self):
        for category in set(ErrorCategory) - _LONG_OPEN_CATEGORIES:
            assert self._tripped_cap(category) == _MAX_OPEN_S, category

    def test_entitlement_is_not_merely_permanent(self):
        # The distinction that motivates the category: PERMANENT would have
        # given an entitlement denial the 30-minute cap, re-probing a dead
        # provider 8x more often than the fix does.
        assert self._tripped_cap(ErrorCategory.NOT_ENTITLED) > self._tripped_cap(
            ErrorCategory.PERMANENT
        )
