"""Tests for compute-routing type definitions."""

import dataclasses

import pytest

from genesis.routing.types import (
    BudgetStatus,
    CallResult,
    CallSiteConfig,
    DegradationLevel,
    ErrorCategory,
    ProviderConfig,
    ProviderState,
    RetryPolicy,
    RoutingConfig,
    RoutingResult,
)

# ─── StrEnum values ─────────────────────────────────────────────────────────


def test_provider_state_values():
    assert ProviderState.CLOSED == "closed"
    assert ProviderState.OPEN == "open"
    assert ProviderState.HALF_OPEN == "half_open"


def test_error_category_values():
    assert ErrorCategory.TRANSIENT == "transient"
    assert ErrorCategory.DEGRADED == "degraded"
    assert ErrorCategory.PERMANENT == "permanent"


def test_degradation_level_values():
    assert DegradationLevel.NORMAL == "L0"
    assert DegradationLevel.LOCAL_COMPUTE_DOWN == "L5"
    assert len(DegradationLevel) == 6


def test_budget_status_values():
    assert BudgetStatus.UNDER_LIMIT == "under_limit"
    assert BudgetStatus.WARNING == "warning"
    assert BudgetStatus.EXCEEDED == "exceeded"


# ─── Frozen dataclasses ─────────────────────────────────────────────────────


def test_provider_config_frozen():
    pc = ProviderConfig(
        name="ollama", provider_type="local", model_id="qwen3",
        is_free=True, rpm_limit=None, open_duration_s=60,
    )
    assert pc.name == "ollama"
    with pytest.raises(dataclasses.FrozenInstanceError):
        pc.name = "other"  # type: ignore[misc]


def test_call_site_config_defaults():
    cs = CallSiteConfig(id="micro", chain=["ollama", "anthropic"])
    assert cs.default_paid is False
    assert cs.never_pays is False
    assert cs.retry_profile == "default"
    # dispatch defaults to "dual" so every existing call site without
    # an explicit value preserves the pre-F1 (API-first-then-CLI)
    # behaviour.  Regression guard for the "absence of dispatch = no
    # behaviour change" contract.
    assert cs.dispatch == "dual"


def test_retry_policy_defaults():
    rp = RetryPolicy()
    assert rp.max_retries == 3
    assert rp.base_delay_ms == 500
    assert rp.max_delay_ms == 30000
    assert rp.backoff_multiplier == 2.0
    assert rp.jitter_pct == 0.25


def test_call_result_defaults():
    cr = CallResult(success=True, content="hello")
    assert cr.input_tokens == 0
    assert cr.cost_usd == 0.0
    assert cr.retry_after_s is None
    with pytest.raises(dataclasses.FrozenInstanceError):
        cr.success = False  # type: ignore[misc]


def test_routing_result_defaults():
    rr = RoutingResult(success=False, call_site_id="test", error="boom")
    assert rr.attempts == 0
    assert rr.fallback_used is False
    assert rr.dead_lettered is False


def test_routing_config_frozen():
    rc = RoutingConfig(providers={}, call_sites={}, retry_profiles={})
    with pytest.raises(dataclasses.FrozenInstanceError):
        rc.providers = {"x": None}  # type: ignore[misc]
