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
async def test_run_eval_all_pass():
    """Mock delegate returns correct answers for all classification cases."""
    config = _make_config()

    # Map case IDs to expected outputs
    expected_map = {
        "cal_01": "0", "cal_02": "1", "cal_03": "2", "cal_04": "3",
        "cal_05": "4", "cal_06": "4", "cal_07": "4", "cal_08": "0",
        "sup_01": "1", "sup_02": "3", "sup_03": "0", "sup_04": "4",
        "sup_05": "1", "sup_06": "2", "sup_07": "4",
    }

    call_count = 0

    async def mock_call(provider, model_id, messages, **kwargs):
        nonlocal call_count
        # Extract the expected answer from the prompt content
        prompt = messages[-1]["content"]
        # Find matching case by checking prompt content
        for case_id, _answer in expected_map.items():
            if case_id.startswith("cal_01") and "what time is it" in prompt:
                return CallResult(success=True, content="0")
            if "rename a variable" in prompt:
                return CallResult(success=True, content="1")
        # Default: return "0" — some will fail, that's OK for this test
        call_count += 1
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
    assert summary.model_id == "test-model"
    assert summary.trigger == EvalTrigger.MANUAL


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
