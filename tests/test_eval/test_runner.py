"""Tests for eval runner — uses mock LiteLLM delegate."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from genesis.eval.runner import run_eval
from genesis.eval.types import EvalTrigger
from genesis.routing.types import CallResult, ProviderConfig, RetryPolicy, RoutingConfig


def _make_config(provider_name: str = "test-provider") -> RoutingConfig:
    """Build a minimal RoutingConfig for testing."""
    return RoutingConfig(
        providers={
            provider_name: ProviderConfig(
                name=provider_name,
                provider_type="openai",
                model_id="test-model",
                is_free=True,
                rpm_limit=None,
                open_duration_s=60,
                profile="test-profile",
            ),
        },
        call_sites={},
        retry_profiles={"default": RetryPolicy()},
    )


@pytest.mark.asyncio
async def test_run_eval_completes_and_scores():
    """Mock delegate returns fixed answer; verify scoring works correctly."""
    config = _make_config()

    async def mock_call(provider, model_id, messages, **kwargs):
        # Always return "0" — matches depth-0 cases, fails others
        return CallResult(success=True, content="0")

    with patch("genesis.eval.runner.LiteLLMDelegate") as MockDelegate:
        instance = MockDelegate.return_value
        instance.call = mock_call
        summary = await run_eval(
            provider_name="test-provider",
            dataset_name="classification",
            config=config,
        )

    assert summary.total_cases == 15
    assert summary.model_id == "test-provider"
    assert summary.trigger == EvalTrigger.MANUAL
    # "0" matches cal_01 (0), cal_08 (0), sup_03 (0) = 3 passes
    assert summary.passed_cases == 3
    assert summary.failed_cases == 12
    assert summary.skipped_cases == 0


@pytest.mark.asyncio
async def test_run_eval_provider_error():
    """Provider errors are counted as skipped, not crashes."""
    config = _make_config()

    async def mock_call(provider, model_id, messages, **kwargs):
        return CallResult(success=False, error="rate limited", status_code=429)

    with patch("genesis.eval.runner.LiteLLMDelegate") as MockDelegate:
        instance = MockDelegate.return_value
        instance.call = mock_call
        summary = await run_eval(
            provider_name="test-provider",
            dataset_name="classification",
            config=config,
        )

    assert summary.total_cases == 15
    assert summary.skipped_cases == 15
    assert summary.passed_cases == 0


@pytest.mark.asyncio
async def test_run_eval_unknown_provider():
    config = _make_config("other")
    with pytest.raises(ValueError, match="unknown provider"):
        await run_eval(
            provider_name="nonexistent",
            dataset_name="classification",
            config=config,
        )
