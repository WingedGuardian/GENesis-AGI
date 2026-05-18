"""Tests for ModelEvalExecutor — pre-flight provider check + execution."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from genesis.eval.surplus_executor import ModelEvalExecutor
from genesis.surplus.types import ComputeTier, SurplusTask, TaskStatus, TaskType


def _make_task(model_id: str = "test-provider", datasets: list[str] | None = None):
    payload = {"model_id": model_id}
    if datasets:
        payload["datasets"] = datasets
    return SurplusTask(
        id="st-eval-1",
        task_type=TaskType.MODEL_EVAL,
        compute_tier=ComputeTier.FREE_API,
        priority=0.5,
        drive_alignment="competence",
        status=TaskStatus.RUNNING,
        created_at="2026-05-17T10:00:00+00:00",
        payload=json.dumps(payload),
    )


class TestPreFlightProviderCheck:
    """Tests for the pre-flight routing config validation."""

    @pytest.mark.asyncio
    async def test_unknown_provider_skipped_gracefully(self):
        """Provider not in routing config → success=True, skip message."""
        mock_config = MagicMock()
        mock_config.providers = {"openrouter": MagicMock(), "deepinfra": MagicMock()}

        with patch(
            "genesis.routing.config.load_config",
            return_value=mock_config,
        ):
            executor = ModelEvalExecutor(db=None)
            result = await executor.execute(_make_task(model_id="unknown-provider"))

        assert result.success is True
        assert "skipped" in result.content
        assert "unknown-provider" in result.content
        assert "not in router config" in result.content

    @pytest.mark.asyncio
    async def test_known_provider_proceeds_to_run(self):
        """Provider in routing config → proceeds to run assessment."""
        mock_config = MagicMock()
        mock_config.providers = {"test-provider": MagicMock()}

        mock_summary = MagicMock()
        mock_summary.aggregate_score = 0.85
        mock_summary.passed_cases = 17
        mock_summary.total_cases = 20
        mock_summary.dataset = "classification"
        mock_summary.duration_s = 12.3

        with (
            patch(
                "genesis.routing.config.load_config",
                return_value=mock_config,
            ),
            patch(
                "genesis.eval.surplus_executor.run_eval",
                new_callable=AsyncMock,
                return_value=mock_summary,
            ),
            patch(
                "genesis.eval.surplus_executor.list_datasets",
                return_value=["classification"],
            ),
        ):
            executor = ModelEvalExecutor(db=None)
            result = await executor.execute(_make_task(model_id="test-provider"))

        assert result.success is True
        assert "test-provider" in result.content
        assert "17/20" in result.content

    @pytest.mark.asyncio
    async def test_config_load_failure_falls_through(self):
        """If config load raises, execution continues (no hard failure)."""
        mock_summary = MagicMock()
        mock_summary.aggregate_score = 0.9
        mock_summary.passed_cases = 9
        mock_summary.total_cases = 10
        mock_summary.dataset = "reasoning"
        mock_summary.duration_s = 5.0

        with (
            patch(
                "genesis.routing.config.load_config",
                side_effect=FileNotFoundError("config not found"),
            ),
            patch(
                "genesis.eval.surplus_executor.run_eval",
                new_callable=AsyncMock,
                return_value=mock_summary,
            ),
            patch(
                "genesis.eval.surplus_executor.list_datasets",
                return_value=["reasoning"],
            ),
        ):
            executor = ModelEvalExecutor(db=None)
            result = await executor.execute(_make_task(model_id="any-provider"))

        # Should succeed — config failure doesn't block execution
        assert result.success is True
        assert "any-provider" in result.content


class TestPayloadValidation:
    """Tests for payload parsing edge cases."""

    @pytest.mark.asyncio
    async def test_missing_model_id_returns_error(self):
        task = SurplusTask(
            id="st-eval-2",
            task_type=TaskType.MODEL_EVAL,
            compute_tier=ComputeTier.FREE_API,
            priority=0.5,
            drive_alignment="competence",
            status=TaskStatus.RUNNING,
            created_at="2026-05-17T10:00:00+00:00",
            payload=json.dumps({"datasets": ["classification"]}),
        )
        executor = ModelEvalExecutor(db=None)
        result = await executor.execute(task)

        assert result.success is False
        assert "missing 'model_id'" in result.error

    @pytest.mark.asyncio
    async def test_none_payload_returns_error(self):
        task = SurplusTask(
            id="st-eval-3",
            task_type=TaskType.MODEL_EVAL,
            compute_tier=ComputeTier.FREE_API,
            priority=0.5,
            drive_alignment="competence",
            status=TaskStatus.RUNNING,
            created_at="2026-05-17T10:00:00+00:00",
            payload=None,
        )
        executor = ModelEvalExecutor(db=None)
        result = await executor.execute(task)

        assert result.success is False
        assert "missing 'model_id'" in result.error

    @pytest.mark.asyncio
    async def test_invalid_json_payload_returns_error(self):
        task = SurplusTask(
            id="st-eval-4",
            task_type=TaskType.MODEL_EVAL,
            compute_tier=ComputeTier.FREE_API,
            priority=0.5,
            drive_alignment="competence",
            status=TaskStatus.RUNNING,
            created_at="2026-05-17T10:00:00+00:00",
            payload="not-valid-json{{{",
        )
        executor = ModelEvalExecutor(db=None)
        result = await executor.execute(task)

        assert result.success is False
        assert "missing 'model_id'" in result.error


class TestRunExecution:
    """Tests for the run logic after pre-flight passes."""

    @pytest.mark.asyncio
    async def test_all_datasets_fail_returns_error(self):
        mock_config = MagicMock()
        mock_config.providers = {"failing-provider": MagicMock()}

        with (
            patch(
                "genesis.routing.config.load_config",
                return_value=mock_config,
            ),
            patch(
                "genesis.eval.surplus_executor.run_eval",
                new_callable=AsyncMock,
                side_effect=RuntimeError("provider down"),
            ),
            patch(
                "genesis.eval.surplus_executor.list_datasets",
                return_value=["classification"],
            ),
        ):
            executor = ModelEvalExecutor(db=None)
            result = await executor.execute(_make_task(model_id="failing-provider"))

        assert result.success is False
        assert "all eval datasets failed" in result.error
        assert "provider down" in result.error

    @pytest.mark.asyncio
    async def test_no_datasets_available_returns_error(self):
        mock_config = MagicMock()
        mock_config.providers = {"test-provider": MagicMock()}

        with (
            patch(
                "genesis.routing.config.load_config",
                return_value=mock_config,
            ),
            patch(
                "genesis.eval.surplus_executor.list_datasets",
                return_value=[],
            ),
        ):
            executor = ModelEvalExecutor(db=None)
            result = await executor.execute(_make_task(model_id="test-provider"))

        assert result.success is False
        assert "no eval datasets" in result.error

    @pytest.mark.asyncio
    async def test_explicit_datasets_used_over_list(self):
        """When payload includes datasets list, use those instead of list_datasets()."""
        mock_config = MagicMock()
        mock_config.providers = {"test-provider": MagicMock()}

        mock_summary = MagicMock()
        mock_summary.aggregate_score = 0.75
        mock_summary.passed_cases = 15
        mock_summary.total_cases = 20
        mock_summary.dataset = "custom-set"
        mock_summary.duration_s = 8.0

        with (
            patch(
                "genesis.routing.config.load_config",
                return_value=mock_config,
            ),
            patch(
                "genesis.eval.surplus_executor.run_eval",
                new_callable=AsyncMock,
                return_value=mock_summary,
            ) as mock_run,
            patch(
                "genesis.eval.surplus_executor.list_datasets",
                return_value=["should-not-be-called"],
            ),
        ):
            executor = ModelEvalExecutor(db=None)
            task = _make_task(model_id="test-provider", datasets=["custom-set"])
            result = await executor.execute(task)

        # Should have called run with "custom-set", not "should-not-be-called"
        mock_run.assert_called_once()
        call_kwargs = mock_run.call_args
        assert call_kwargs[1]["dataset_name"] == "custom-set"
        assert result.success is True
