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
    probe therefore "confirmed health", closed the breaker, fired on_recovery,
    and `ProviderEscalation.record_recovery` RESOLVED the provider_failure
    observation and cleared its `first_trip_at`.

    MEASURED over the live 3-day outage: FIVE separate `provider_failure`
    observations, each carrying a fresh `first_trip_at`, four auto-resolved this
    way. So the longest duration any surface could report was the time since the
    last false heal, never the real age of the outage. The reset cadence was
    ~9-12h, NOT once per probe cycle: `events` shows consecutive trips
    5/5/10/20/35/65/128 min apart, i.e. the backoff escalated normally between
    resets.

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
    QUOTA_EXHAUSTED (`retry.py`), and on the install where this was diagnosed the
    persisted breaker state for the affected provider carried exactly that
    category. So this is the case that actually fired, and it must not heal on a
    listing either: an exhausted or entitlement-blocked account still serves
    `/v1/models` perfectly.
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
    a real completion closes the breaker exactly as before.

    Scope note, stated honestly: this also passes with the guard removed, because
    it exercises `record_success`, which the guard does not touch, and it gives
    the provider TRAFFIC. It pins the recovery CONTRACT, not the guard. The
    no-traffic stranding risk — the one that actually bites — is covered by the
    restart test at the bottom of this file.
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


def test_a_restart_does_not_carry_a_stale_category_onto_a_closed_breaker(tmp_path):
    """A qualifier must not outlive the state it qualifies.

    `load_state` restores `_state` only when the saved value was OPEN, but
    restored `_last_failure_category` UNCONDITIONALLY. So a breaker persisted
    while HALF_OPEN came back CLOSED still carrying "permanent" — and the
    probe-heal guard then read that dead fact from a previous process forever.

    That is reachable in normal operation: the `.state` property mutates
    OPEN -> HALF_OPEN as a side effect of being READ (every routing decision
    reads it), and `save_state` serialises every breaker whenever any one of
    them changes.

    Consequence, and the reason this test exists: the guard would strand a
    HEALTHY provider in HALF_OPEN indefinitely once a probe blip suspected it,
    with no real traffic to heal it — which is precisely the case
    `record_probe_success` was written to rescue, and precisely what the guard's
    own comment claims cannot happen.
    """
    import json

    state_file = tmp_path / "cb.json"
    state_file.write_text(json.dumps({
        "free-1": {
            "state": "half_open",           # what a live read leaves behind
            "consecutive_failures": 0,
            "trip_count": 1,
            "last_failure_category": "permanent",
        }
    }))
    prov = _provider("free-1")
    reg = CircuitBreakerRegistry(
        {"free-1": prov}, clock=lambda: 0.0, state_file=state_file, persist=False,
    )
    cb = reg.get("free-1")
    assert cb.state == ProviderState.CLOSED, "precondition: a non-OPEN save restores CLOSED"
    assert cb._last_failure_category is None, (
        "a category was carried onto a CLOSED breaker — it qualifies a FAILING "
        "breaker, and surviving the state it describes makes it a poison pill "
        "that permanently disables probe healing"
    )

    # And the behaviour that guard-poisoning would break: a probe blip must
    # still be able to heal a provider that is not actually failing.
    cb.probe_suspect()
    assert cb.state == ProviderState.HALF_OPEN
    for _ in range(3):
        cb.record_probe_success()
    assert cb.state == ProviderState.CLOSED, (
        "a healthy provider could not heal after a restart — stranded in "
        "HALF_OPEN with no traffic to rescue it"
    )


def test_degraded_responses_are_not_healed_by_a_listing_probe():
    """Codex P2, confirmed: DEGRADED was missing from the original two-value guard.

    `retry.py::classify_error` returns DEGRADED for malformed/partial/truncated
    completions, and `router.py` records it on the breaker like any other health
    failure (only RATE_LIMITED and BAD_REQUEST bypass it). So a provider whose
    completions come back truncated could still be healed by three clean
    model-listing probes — the same false-heal this change exists to remove,
    reached through a category the first fix did not enumerate.

    The guard is an ALLOWLIST now rather than a blocklist, so the question is
    "does reachability actually evidence recovery here?" — true only for
    TRANSIENT/TIMEOUT — and a category added later is blocked by default.
    """
    reg, prov = _half_open_with(ErrorCategory.DEGRADED)
    checker = ProviderHealthChecker(_config(prov), breakers=reg)
    _clean_probe(checker)
    for _ in range(5):
        checker._sync_to_breakers()
    assert reg.get("free-1").state == ProviderState.HALF_OPEN, (
        "a listing probe healed a DEGRADED provider — reachability is not "
        "evidence that completions have stopped being malformed"
    )


def test_the_probe_heal_allowlist_is_the_complete_reachability_set():
    """Lock the WHOLE set, not one member at a time.

    Enumerated against ErrorCategory so a new member cannot silently join the
    healable set: a probe answers "is the endpoint reachable and the model
    listed", which evidences recovery only for failures that were themselves
    about reachability.
    """
    from genesis.routing.circuit_breaker import _PROBE_HEALABLE

    assert set(_PROBE_HEALABLE) == {ErrorCategory.TRANSIENT, ErrorCategory.TIMEOUT}
    not_healable = set(ErrorCategory) - _PROBE_HEALABLE
    assert not_healable == {
        ErrorCategory.DEGRADED,
        ErrorCategory.PERMANENT,
        ErrorCategory.QUOTA_EXHAUSTED,
        ErrorCategory.RATE_LIMITED,
        ErrorCategory.BAD_REQUEST,
    }, (
        "ErrorCategory changed — decide explicitly whether a LISTING probe is "
        "evidence of recovery for the new member, then update this assertion"
    )


def test_sub_threshold_failures_do_not_poison_the_probe_heal_guard():
    """Cross-model review (Kimi K3), reproduced: the guard stranded HEALTHY providers.

    `record_failure` sets `_last_failure_category` on EVERY failure
    (circuit_breaker.py:246), before the threshold check — so one or two 403s
    below `failure_threshold=3` leave "permanent" sitting on a breaker that
    never tripped and is still CLOSED. `probe_suspect()` then moves CLOSED ->
    HALF_OPEN on any failed or rate-limited probe WITHOUT clearing it
    (`provider_health.py` routes both there), and the guard refused to heal.

    With no real traffic — exactly the population `record_probe_success` exists
    to rescue — the provider is stranded in HALF_OPEN until the process
    restarts. `snapshots/call_sites.py:162` renders HALF_OPEN as degraded, so
    this branch's own duration suffix would then report `(ongoing for 9d)` for a
    provider that is completely fine. That is the same false-degraded-forever
    failure this branch exists to remove, recreated one layer over.

    The fix is to ask whether a non-healable category actually TRIPPED the
    breaker (`_trip_count > 0`), rather than whether one was merely recorded.
    Clearing the category in `probe_suspect()` was the other candidate and was
    rejected: `_effective_open_duration()` also reads it to pick the 4h quota
    cap, so clearing it there would silently shorten a quota-dead provider's
    backoff — trading this bug for a quieter one.
    """
    prov = _provider("free-1")
    reg = CircuitBreakerRegistry({"free-1": prov}, clock=lambda: 0.0, persist=False)
    cb = reg.get("free-1")

    # Two sub-threshold PERMANENT failures: below failure_threshold=3, so the
    # breaker never trips and stays CLOSED — but the category is now set.
    for _ in range(2):
        cb.record_failure(ErrorCategory.PERMANENT)
    assert cb.state == ProviderState.CLOSED, "precondition: sub-threshold, still closed"
    assert cb._last_failure_category == ErrorCategory.PERMANENT
    assert cb._trip_count == 0, "precondition: never tripped"

    # A probe blip (unreachable, or a 429 on the listing endpoint) suspects it.
    checker = ProviderHealthChecker(_config(prov), breakers=reg)
    checker._results = {
        "free-1": ProviderProbeResult(
            provider_name="free-1", reachable=False, configured=True,
            error="ConnectionError",
        ),
    }
    checker._sync_to_breakers()
    assert reg.get("free-1").state == ProviderState.HALF_OPEN

    # Clean probes must now heal it: nothing ever tripped this breaker.
    _clean_probe(checker)
    for _ in range(3):
        checker._sync_to_breakers()
    assert reg.get("free-1").state == ProviderState.CLOSED, (
        "a healthy provider was stranded in HALF_OPEN by a category from "
        "sub-threshold failures that never tripped the breaker"
    )
