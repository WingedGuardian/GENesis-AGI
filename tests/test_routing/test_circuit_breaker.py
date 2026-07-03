"""Tests for circuit breaker."""

from __future__ import annotations

import json

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


def test_probe_success_heals_half_open_after_threshold():
    cb, _ = _half_open_breaker(probe_threshold=3)
    cb.record_probe_success()
    assert cb.state == ProviderState.HALF_OPEN  # 1 < 3
    cb.record_probe_success()
    assert cb.state == ProviderState.HALF_OPEN  # 2 < 3
    cb.record_probe_success()
    assert cb.state == ProviderState.CLOSED     # 3 → healed
    assert cb.trip_count == 0


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


def test_probe_success_fires_on_recovery():
    """On full recovery the breaker fires on_recovery — this is what drives
    #698's escalation observation-resolve, so a probe-healed provider does not
    leave a stale 'provider_failure' observation."""
    recovered = []
    t = [0.0]
    cb = CircuitBreaker(
        _provider("p1"), failure_threshold=2, open_duration_s=10,
        clock=lambda: t[0], probe_success_threshold=2,
        on_recovery=lambda name: recovered.append(name),
    )
    cb.record_failure(ErrorCategory.TRANSIENT)
    cb.record_failure(ErrorCategory.TRANSIENT)
    t[0] = 100.0
    assert cb.state == ProviderState.HALF_OPEN
    cb.record_probe_success()
    assert recovered == []  # below threshold — no recovery yet
    cb.record_probe_success()
    assert cb.state == ProviderState.CLOSED
    assert recovered == ["p1"]


def test_real_failure_after_probe_heal_can_retrip():
    """A provider healed via probe must still re-trip on real failures —
    healing must not disable failure tracking."""
    cb, _ = _half_open_breaker(probe_threshold=2)
    cb.record_probe_success()
    cb.record_probe_success()
    assert cb.state == ProviderState.CLOSED
    cb.record_failure(ErrorCategory.TRANSIENT)
    cb.record_failure(ErrorCategory.TRANSIENT)  # failure_threshold=2
    assert cb.state == ProviderState.OPEN


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
