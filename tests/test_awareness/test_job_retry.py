"""Tests for JobRetryRegistry — circuit breaker for scheduled job retries."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from genesis.awareness.job_retry import JobRetryRegistry, RetryResult


@pytest.fixture
def registry():
    return JobRetryRegistry()


@pytest.fixture
def success_fn():
    return AsyncMock()


@pytest.fixture
def failing_fn():
    fn = AsyncMock(side_effect=RuntimeError("job failed"))
    return fn


class TestRegistration:
    def test_register_and_list(self, registry, success_fn):
        registry.register("job_a", success_fn)
        registry.register("job_b", success_fn)
        assert sorted(registry.list_registered()) == ["job_a", "job_b"]


class TestRetrySuccess:
    @pytest.mark.asyncio
    async def test_successful_retry(self, registry, success_fn):
        registry.register("my_job", success_fn)
        result = await registry.attempt_retry("my_job")
        assert result.result == RetryResult.RETRIED
        assert "succeeded" in result.message
        success_fn.assert_called_once()

    @pytest.mark.asyncio
    async def test_resets_state_on_success(self, registry, success_fn):
        registry.register("my_job", success_fn)
        await registry.attempt_retry("my_job")
        # After success, retry_count should be reset
        state = registry._jobs["my_job"]
        assert state.retry_count == 0
        assert state.last_retry_at is None


class TestRetryFailure:
    @pytest.mark.asyncio
    async def test_failed_retry_increments_count(self, registry, failing_fn):
        registry.register("my_job", failing_fn, backoff_base_s=0)
        result = await registry.attempt_retry("my_job")
        assert result.result == RetryResult.RETRIED
        assert result.retry_count == 1


class TestBackoff:
    @pytest.mark.asyncio
    async def test_backoff_enforced(self, registry, failing_fn):
        registry.register("my_job", failing_fn, backoff_base_s=3600)
        # First retry — succeeds to execute (but job fails)
        await registry.attempt_retry("my_job")
        # Second retry — should be backing off (3600 * 3^1 = 10800s)
        result = await registry.attempt_retry("my_job")
        assert result.result == RetryResult.BACKING_OFF


class TestQuarantine:
    @pytest.mark.asyncio
    async def test_quarantine_after_max_retries(self, registry, failing_fn):
        registry.register("my_job", failing_fn, max_retries=2, backoff_base_s=0)
        await registry.attempt_retry("my_job")  # attempt 1
        await registry.attempt_retry("my_job")  # attempt 2
        result = await registry.attempt_retry("my_job")  # attempt 3 → quarantine
        assert result.result == RetryResult.QUARANTINED
        assert registry.is_quarantined("my_job")

    @pytest.mark.asyncio
    async def test_quarantined_job_stays_quarantined(self, registry, failing_fn):
        registry.register("my_job", failing_fn, max_retries=1, backoff_base_s=0)
        await registry.attempt_retry("my_job")
        await registry.attempt_retry("my_job")  # quarantine
        result = await registry.attempt_retry("my_job")  # still quarantined
        assert result.result == RetryResult.QUARANTINED

    @pytest.mark.asyncio
    async def test_manual_unquarantine(self, registry, failing_fn):
        registry.register("my_job", failing_fn, max_retries=1, backoff_base_s=0)
        await registry.attempt_retry("my_job")
        await registry.attempt_retry("my_job")  # quarantine
        assert registry.is_quarantined("my_job")

        result = registry.unquarantine("my_job")
        assert result is True
        assert not registry.is_quarantined("my_job")

    @pytest.mark.asyncio
    async def test_auto_unquarantine(self, registry, success_fn):
        registry.register("my_job", success_fn, max_retries=1, backoff_base_s=0)
        state = registry._jobs["my_job"]
        # Simulate quarantine from 25 hours ago
        state.quarantined_at = (datetime.now(UTC) - timedelta(hours=25)).isoformat()
        state.quarantine_reason = "test"
        state.retry_count = 5

        result = await registry.attempt_retry("my_job")
        assert result.result == RetryResult.RETRIED
        assert not registry.is_quarantined("my_job")

    def test_unquarantine_nonexistent_returns_false(self, registry):
        assert registry.unquarantine("nonexistent") is False


class TestNotRegistered:
    @pytest.mark.asyncio
    async def test_unregistered_job(self, registry):
        result = await registry.attempt_retry("unknown_job")
        assert result.result == RetryResult.NOT_REGISTERED

    def test_is_quarantined_unregistered(self, registry):
        assert not registry.is_quarantined("unknown_job")
