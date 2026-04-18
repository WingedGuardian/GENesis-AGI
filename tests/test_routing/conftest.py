"""Shared fixtures for routing tests."""

from unittest.mock import patch

import pytest

from genesis.routing.types import (
    CallResult,
    CallSiteConfig,
    ProviderConfig,
    RetryPolicy,
    RoutingConfig,
)


@pytest.fixture(autouse=True)
def _skip_api_key_check():
    """Tests don't have real API keys — bypass the check in config loading."""
    with patch("genesis.observability.snapshots.api_keys.has_api_key", return_value=True):
        yield


class MockDelegate:
    """Mock LLM delegate for testing the router."""

    def __init__(self, responses=None):
        self.responses: dict[str, CallResult] = responses or {}
        self.calls: list[dict] = []

    async def call(self, provider, model_id, messages, **kwargs):
        self.calls.append({"provider": provider, "model_id": model_id, "messages": messages})
        if provider in self.responses:
            return self.responses[provider]
        return CallResult(success=True, content="mock response", cost_usd=0.01)


@pytest.fixture
def sample_providers():
    return {
        "free-1": ProviderConfig(name="free-1", provider_type="mistral", model_id="mistral-large", is_free=True, rpm_limit=2, open_duration_s=120),
        "free-2": ProviderConfig(name="free-2", provider_type="groq", model_id="llama-70b", is_free=True, rpm_limit=30, open_duration_s=120),
        "paid-1": ProviderConfig(name="paid-1", provider_type="anthropic", model_id="claude-haiku", is_free=False, rpm_limit=None, open_duration_s=120),
        "paid-2": ProviderConfig(name="paid-2", provider_type="openai", model_id="gpt-5-nano", is_free=False, rpm_limit=None, open_duration_s=120),
    }


@pytest.fixture
def sample_config(sample_providers):
    return RoutingConfig(
        providers=sample_providers,
        call_sites={
            "test_mixed": CallSiteConfig(id="test_mixed", chain=["free-1", "free-2", "paid-1"]),
            "test_paid": CallSiteConfig(id="test_paid", chain=["paid-1", "paid-2"], default_paid=True),
            "test_never_pays": CallSiteConfig(id="test_never_pays", chain=["free-1", "free-2"], never_pays=True),
        },
        retry_profiles={"default": RetryPolicy(max_retries=1, base_delay_ms=10, jitter_pct=0.0)},
    )
