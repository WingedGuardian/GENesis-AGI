"""ProviderHealthChecker._sync_to_breakers heals a HALF_OPEN breaker on a clean
probe (the low/no-traffic recovery path) and still only downgrades on failure.
"""

from __future__ import annotations

from genesis.observability.provider_health import (
    ProviderHealthChecker,
    ProviderProbeResult,
)
from genesis.routing.circuit_breaker import CircuitBreakerRegistry
from genesis.routing.types import (
    ErrorCategory,
    ProviderConfig,
    ProviderState,
    RoutingConfig,
)


def _provider(name: str = "free-1") -> ProviderConfig:
    return ProviderConfig(
        name=name, provider_type="groq", model_id="m",
        is_free=True, rpm_limit=None, open_duration_s=10,
    )


def _config(provider: ProviderConfig) -> RoutingConfig:
    return RoutingConfig(
        providers={provider.name: provider}, call_sites={}, retry_profiles={},
    )


def _half_open_registry():
    """Registry with free-1 driven to HALF_OPEN."""
    t = [0.0]
    prov = _provider("free-1")
    reg = CircuitBreakerRegistry({"free-1": prov}, clock=lambda: t[0], persist=False)
    cb = reg.get("free-1")
    for _ in range(3):  # default failure_threshold = 3
        cb.record_failure(ErrorCategory.TRANSIENT)
    assert cb.state == ProviderState.OPEN
    t[0] = 100.0  # past the open window
    assert cb.state == ProviderState.HALF_OPEN
    return reg, prov


def test_clean_probe_heals_half_open_breaker():
    """A reachable + model_available probe on a HALF_OPEN provider advances it to
    CLOSED via record_probe_success after the default probe threshold (3)."""
    reg, prov = _half_open_registry()
    checker = ProviderHealthChecker(_config(prov), breakers=reg)
    checker._results = {
        "free-1": ProviderProbeResult(
            provider_name="free-1", reachable=True, configured=True,
            model_available=True,
        ),
    }
    # default probe_success_threshold = 3 → three clean syncs heal it.
    for _ in range(3):
        checker._sync_to_breakers()
    assert reg.get("free-1").state == ProviderState.CLOSED


def test_probe_model_unavailable_does_not_heal():
    """Reachable but model_available is False/None (endpoint up but this model
    not listed) must NOT heal — weaker signal than a real completion."""
    reg, prov = _half_open_registry()
    checker = ProviderHealthChecker(_config(prov), breakers=reg)
    checker._results = {
        "free-1": ProviderProbeResult(
            provider_name="free-1", reachable=True, configured=True,
            model_available=None,
        ),
    }
    for _ in range(5):
        checker._sync_to_breakers()
    assert reg.get("free-1").state == ProviderState.HALF_OPEN


def test_unreachable_probe_still_only_downgrades():
    """Regression: an unreachable probe must still only downgrade a CLOSED
    breaker to HALF_OPEN (probe_suspect), never heal."""
    prov = _provider("free-1")
    reg = CircuitBreakerRegistry({"free-1": prov}, clock=lambda: 0, persist=False)
    assert reg.get("free-1").state == ProviderState.CLOSED
    checker = ProviderHealthChecker(_config(prov), breakers=reg)
    checker._results = {
        "free-1": ProviderProbeResult(
            provider_name="free-1", reachable=False, configured=True,
            error="ConnectionError",
        ),
    }
    checker._sync_to_breakers()
    assert reg.get("free-1").state == ProviderState.HALF_OPEN


def _half_open_with(category: ErrorCategory):
    """Registry with free-1 driven to HALF_OPEN by a specific failure category."""
    t = [0.0]
    prov = _provider("free-1")
    reg = CircuitBreakerRegistry({"free-1": prov}, clock=lambda: t[0], persist=False)
    cb = reg.get("free-1")
    for _ in range(3):
        cb.record_failure(category)
    assert cb.state == ProviderState.OPEN
    t[0] = 100000.0  # past the open window (quota cap is 4h, base 10s here)
    assert cb.state == ProviderState.HALF_OPEN
    return reg, prov


def _clean_probe(checker: ProviderHealthChecker) -> None:
    checker._results = {
        "free-1": ProviderProbeResult(
            provider_name="free-1", reachable=True, configured=True,
            model_available=True,
        ),
    }


def test_entitlement_failure_is_not_healed_by_a_listing_probe():
    """A model being LISTED is not evidence it is CALLABLE.

    Live incident 2026-08-28/29: `mistral-large-latest` stayed listed by
    `GET /v1/models` while every real call returned 403 `tier_not_allowed`. The
    probe therefore "confirmed health" three times per cycle, closed the breaker,
    fired on_recovery, and `ProviderEscalation.record_recovery` RESOLVED the
    provider_failure observation and cleared its `first_trip_at`. A 3-day
    permanent outage was rendered as a series of ~40-minute incidents that each
    recovered, which is why nothing ever reported a multi-day duration.

    A listing probe is weaker evidence than a real completion for ANY failure,
    but for a PERMANENT one it is not evidence at all — the endpoint being up is
    exactly what a 403-on-use looks like.
    """
    reg, prov = _half_open_with(ErrorCategory.PERMANENT)
    checker = ProviderHealthChecker(_config(prov), breakers=reg)
    _clean_probe(checker)
    for _ in range(5):  # well past probe_success_threshold = 3
        checker._sync_to_breakers()
    assert reg.get("free-1").state == ProviderState.HALF_OPEN, (
        "a listing probe healed a PERMANENT failure — the outage clock resets "
        "and a multi-day outage renders as repeated short incidents"
    )


def test_quota_exhausted_is_not_healed_by_a_listing_probe():
    """The live category for the real incident.

    `classify_error` maps a 403 whose body contains a quota keyword to
    QUOTA_EXHAUSTED (`retry.py`), and the persisted breaker state for
    `mistral-large-free` reads exactly that. So this is the case that actually
    fired in production, and it must not heal on a listing either: an exhausted
    or entitlement-blocked account still serves `/v1/models` perfectly.
    """
    reg, prov = _half_open_with(ErrorCategory.QUOTA_EXHAUSTED)
    checker = ProviderHealthChecker(_config(prov), breakers=reg)
    _clean_probe(checker)
    for _ in range(5):
        checker._sync_to_breakers()
    assert reg.get("free-1").state == ProviderState.HALF_OPEN


def test_a_real_success_still_heals_an_entitlement_failure():
    """The recovery path must stay open — this is the anti-lockout control.

    Blocking the PROBE must not make a dead provider unrecoverable. HALF_OPEN is
    still `is_available()`, so real traffic is attempted; when access is restored
    a real completion closes the breaker exactly as before. Without this test the
    fix above could pass while permanently stranding a recovered provider.
    """
    reg, prov = _half_open_with(ErrorCategory.PERMANENT)
    cb = reg.get("free-1")
    for _ in range(2):  # default success_threshold = 2
        cb.record_success()
    assert cb.state == ProviderState.CLOSED


def test_transient_failure_is_still_healed_by_a_probe():
    """The case probe-healing was BUILT for must keep working.

    A low/no-traffic fallback that went unreachable and came back has no real
    completions to heal itself with; the probe is its only route out of
    HALF_OPEN. Narrowing the block to PERMANENT/QUOTA must not touch this.
    """
    reg, prov = _half_open_with(ErrorCategory.TRANSIENT)
    checker = ProviderHealthChecker(_config(prov), breakers=reg)
    _clean_probe(checker)
    for _ in range(3):
        checker._sync_to_breakers()
    assert reg.get("free-1").state == ProviderState.CLOSED
