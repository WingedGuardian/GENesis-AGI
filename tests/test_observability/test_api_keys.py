"""API-key health: essential-aware alert severity + dashboard color convention."""

from __future__ import annotations

from genesis.observability.snapshots.api_keys import (
    _compute_alert_severity,
    _key_health_color,
)

# --- _compute_alert_severity: critical ONLY when an essential is uncovered ---


def test_quota_exhaustion_silent_when_essentials_covered():
    """OpenRouter-style outage: paid + systemic + out of credits, but the
    essentials are still covered by free providers → SILENT (info), no banner.
    This is the demo scenario — the alarm must not fire."""
    assert _compute_alert_severity(
        status="configured", criticality="systemic", is_free=False,
        cb_state="open", cb_reason="quota_exhausted", essential_uncovered=False,
    ) == "info"


def test_quota_exhaustion_critical_when_essential_uncovered():
    """Same provider state, but an essential site is genuinely uncovered →
    CRITICAL is legitimate."""
    assert _compute_alert_severity(
        status="configured", criticality="systemic", is_free=False,
        cb_state="open", cb_reason="quota_exhausted", essential_uncovered=True,
    ) == "critical"


def test_cb_open_sole_gated_on_essential_coverage():
    base = dict(
        status="configured", criticality="sole", is_free=False,
        cb_state="open", cb_reason="degraded",
    )
    assert _compute_alert_severity(**base, essential_uncovered=False) == "info"
    assert _compute_alert_severity(**base, essential_uncovered=True) == "critical"


def test_dormant_never_alerts():
    assert _compute_alert_severity(
        status="missing", criticality="dormant", is_free=False,
        cb_state="closed", cb_reason=None, essential_uncovered=True,
    ) is None


def test_missing_key_is_warning_not_critical():
    """A missing key is a config gap (surfaced yellow), never a CRITICAL alert —
    the essential-uncovered alarm flows through the degradation banner."""
    assert _compute_alert_severity(
        status="missing", criticality="systemic", is_free=False,
        cb_state="closed", cb_reason=None, essential_uncovered=True,
    ) == "warning"


def test_free_provider_down_silent_when_covered_critical_when_uncovered():
    """Essentials run on FREE providers, so a free provider going down CAN be
    critical — when it uncovers an essential. When covered, it stays silent."""
    base = dict(
        status="configured", criticality="sole", is_free=True,
        cb_state="open", cb_reason="quota_exhausted",
    )
    assert _compute_alert_severity(**base, essential_uncovered=False) == "info"
    assert _compute_alert_severity(**base, essential_uncovered=True) == "critical"


# --- _key_health_color: yellow=missing, red=not-working, green=ok, gray=off ---


def test_color_missing_is_yellow():
    assert _key_health_color("missing", "closed") == "yellow"


def test_color_breaker_open_is_red():
    assert _key_health_color("configured", "open") == "red"


def test_color_validation_failed_is_red():
    assert _key_health_color("failed", "closed") == "red"


def test_color_half_open_is_yellow():
    assert _key_health_color("configured", "half_open") == "yellow"


def test_color_working_is_green():
    assert _key_health_color("validated", "closed") == "green"
    assert _key_health_color("configured", "closed") == "green"


def test_color_local_is_green_disabled_is_gray():
    assert _key_health_color("local", "closed") == "green"
    assert _key_health_color("cc_managed", "closed") == "green"
    assert _key_health_color("disabled", "closed") == "gray"


# --- Integration: real registry + api_key_health + derive_criticality wiring ---


def _routing_config():
    from genesis.routing.types import (
        CallSiteConfig,
        ProviderConfig,
        RetryPolicy,
        RoutingConfig,
    )

    providers = {
        "openrouter-x": ProviderConfig(
            name="openrouter-x", provider_type="openrouter", model_id="m",
            is_free=False, rpm_limit=None, open_duration_s=120,
        ),
        "gemini-free": ProviderConfig(
            name="gemini-free", provider_type="google", model_id="m",
            is_free=True, rpm_limit=None, open_duration_s=120,
        ),
    }
    call_sites = {
        # Essential site: covered by a free provider alongside the paid one.
        "4_light_reflection": CallSiteConfig(
            id="4_light_reflection", chain=["openrouter-x", "gemini-free"],
        ),
        # All-OpenRouter core site → makes openrouter a "sole" provider type,
        # so its quota exhaustion is a critical *candidate* (gated on coverage).
        "17_executor_review": CallSiteConfig(
            id="17_executor_review", chain=["openrouter-x"],
        ),
    }
    return RoutingConfig(
        providers=providers,
        call_sites=call_sites,
        retry_profiles={"default": RetryPolicy(max_retries=1, base_delay_ms=10, jitter_pct=0.0)},
    )


def _registry(config):
    from genesis.routing.circuit_breaker import CircuitBreakerRegistry

    # Inject the essential-site map directly (the synthetic config models one
    # essential site explicitly). build_essential_provider_map is covered in
    # tests/test_routing/test_essential.py.
    return CircuitBreakerRegistry(
        config.providers,
        clock=lambda: 0,
        essential_sites={"4_light_reflection": ["openrouter-x", "gemini-free"]},
    )


def test_api_key_health_openrouter_out_of_credits_essentials_covered(monkeypatch):
    """Demo scenario: OpenRouter out of credits, essential still covered by the
    free provider → OpenRouter shows RED on the API-keys card, but the alert is
    SILENT (info) — no attention-strip banner at all, and overall health is
    unaffected."""
    from genesis.observability.snapshots import api_keys as ak
    from genesis.routing.types import ErrorCategory

    monkeypatch.setenv("API_KEY_OPENROUTER", "sk-test")
    monkeypatch.setenv("GOOGLE_API_KEY", "g-test")
    ak._api_validation_cache.clear()

    config = _routing_config()
    reg = _registry(config)
    for _ in range(3):
        reg.get("openrouter-x").record_failure(ErrorCategory.QUOTA_EXHAUSTED)

    out = ak.api_key_health(config, breakers=reg)
    orp = out["providers"]["openrouter-x"]
    assert orp["key_health"] == "red"          # out of credits → red tile
    assert orp["cb_reason"] == "quota_exhausted"
    assert orp["alert_severity"] == "info"     # silent — essential covered
    assert out["alerts"] == []                 # no banner at all (info not built)


def test_api_key_health_critical_when_essential_uncovered(monkeypatch):
    """When the free provider is ALSO down, the essential site is uncovered →
    OpenRouter's outage is genuinely CRITICAL."""
    from genesis.observability.snapshots import api_keys as ak
    from genesis.routing.types import ErrorCategory

    monkeypatch.setenv("API_KEY_OPENROUTER", "sk-test")
    monkeypatch.setenv("GOOGLE_API_KEY", "g-test")
    ak._api_validation_cache.clear()

    config = _routing_config()
    reg = _registry(config)
    for name in ("openrouter-x", "gemini-free"):
        for _ in range(3):
            reg.get(name).record_failure(ErrorCategory.QUOTA_EXHAUSTED)

    out = ak.api_key_health(config, breakers=reg)
    orp = out["providers"]["openrouter-x"]
    assert orp["key_health"] == "red"
    assert orp["alert_severity"] == "critical"  # essential uncovered → critical
    assert any(a["severity"] == "critical" for a in out["alerts"])
