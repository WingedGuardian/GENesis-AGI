"""JobRetryRegistry — manages automatic retry of failed scheduled jobs.

Self-healing mechanism: when the awareness loop detects a job with
consecutive failures, it attempts retry via registered callables.
Circuit breaker prevents infinite retry loops.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

logger = logging.getLogger(__name__)


class RetryResult(StrEnum):
    """Outcome of a retry attempt."""

    RETRIED = "retried"
    BACKING_OFF = "backing_off"
    QUARANTINED = "quarantined"
    NOT_REGISTERED = "not_registered"


@dataclass
class _RetryState:
    """Per-job retry tracking state."""

    retry_fn: Callable[[], Coroutine]
    max_retries: int = 3
    backoff_base_s: float = 300.0  # 5 minutes
    retry_count: int = 0
    last_retry_at: str | None = None
    quarantined_at: str | None = None
    quarantine_reason: str = ""

    # Auto-unquarantine after this many seconds (default 24h).
    auto_unquarantine_s: float = 86400.0


@dataclass
class RetryAttemptResult:
    """Detailed result of a retry attempt."""

    result: RetryResult
    job_name: str
    message: str = ""
    retry_count: int = 0
    success: bool = False


class JobRetryRegistry:
    """Registry of retriable jobs with circuit-breaker protection.

    Jobs register themselves at startup. The awareness loop calls
    ``attempt_retry()`` when it detects consecutive failures.

    Protection against infinite failure loops:
    - Max retries per failure window (default 3)
    - Exponential backoff: base * 3^retry_count (5m, 15m, 45m)
    - Quarantine after max retries exhausted (24h auto-unquarantine)
    """

    def __init__(self) -> None:
        self._jobs: dict[str, _RetryState] = {}

    def register(
        self,
        job_name: str,
        retry_fn: Callable[[], Coroutine],
        *,
        max_retries: int = 3,
        backoff_base_s: float = 300.0,
    ) -> None:
        """Register a job's retry function.

        Args:
            job_name: Must match the name used in runtime.record_job_success/failure().
            retry_fn: Async callable that re-executes the job.
            max_retries: Max retry attempts before quarantine (default 3).
            backoff_base_s: Base backoff in seconds (default 300 = 5 min).
        """
        self._jobs[job_name] = _RetryState(
            retry_fn=retry_fn,
            max_retries=max_retries,
            backoff_base_s=backoff_base_s,
        )
        logger.debug("Registered retry function for job: %s", job_name)

    async def attempt_retry(self, job_name: str) -> RetryAttemptResult:
        """Attempt to retry a failed job.

        Returns the outcome without raising exceptions.
        """
        state = self._jobs.get(job_name)
        if state is None:
            return RetryAttemptResult(
                result=RetryResult.NOT_REGISTERED,
                job_name=job_name,
                message=f"No retry function registered for {job_name}",
            )

        now = datetime.now(UTC)

        # Check quarantine status (with auto-unquarantine).
        if state.quarantined_at:
            quarantine_dt = datetime.fromisoformat(state.quarantined_at)
            elapsed = (now - quarantine_dt).total_seconds()
            if elapsed < state.auto_unquarantine_s:
                return RetryAttemptResult(
                    result=RetryResult.QUARANTINED,
                    job_name=job_name,
                    retry_count=state.retry_count,
                    message=f"{job_name} is quarantined since {state.quarantined_at}. "
                            f"Auto-unquarantine in {state.auto_unquarantine_s - elapsed:.0f}s. "
                            f"Reason: {state.quarantine_reason}",
                )
            # Auto-unquarantine.
            logger.info("Auto-unquarantining job %s after %ds", job_name, elapsed)
            state.quarantined_at = None
            state.quarantine_reason = ""
            state.retry_count = 0

        # Check retry budget.
        if state.retry_count >= state.max_retries:
            state.quarantined_at = now.isoformat()
            state.quarantine_reason = (
                f"Exhausted {state.max_retries} retries without success"
            )
            logger.warning(
                "Quarantining job %s after %d failed retries",
                job_name, state.retry_count,
            )
            return RetryAttemptResult(
                result=RetryResult.QUARANTINED,
                job_name=job_name,
                retry_count=state.retry_count,
                message=state.quarantine_reason,
            )

        # Check backoff timing.
        if state.last_retry_at:
            last_dt = datetime.fromisoformat(state.last_retry_at)
            backoff_s = state.backoff_base_s * (3 ** state.retry_count)
            elapsed = (now - last_dt).total_seconds()
            if elapsed < backoff_s:
                remaining = backoff_s - elapsed
                return RetryAttemptResult(
                    result=RetryResult.BACKING_OFF,
                    job_name=job_name,
                    retry_count=state.retry_count,
                    message=f"Backing off for {remaining:.0f}s more "
                            f"(attempt {state.retry_count + 1}/{state.max_retries})",
                )

        # Attempt retry.
        state.retry_count += 1
        state.last_retry_at = now.isoformat()
        logger.info(
            "Retrying job %s (attempt %d/%d)",
            job_name, state.retry_count, state.max_retries,
        )

        try:
            await state.retry_fn()
            # Success — reset retry state.
            state.retry_count = 0
            state.last_retry_at = None
            logger.info("Job %s retry succeeded", job_name)
            return RetryAttemptResult(
                result=RetryResult.RETRIED,
                job_name=job_name,
                message="Retry succeeded",
                success=True,
            )
        except Exception as exc:
            logger.error(
                "Job %s retry failed (attempt %d/%d): %s",
                job_name, state.retry_count, state.max_retries, exc,
                exc_info=True,
            )
            return RetryAttemptResult(
                result=RetryResult.RETRIED,
                job_name=job_name,
                retry_count=state.retry_count,
                message=f"Retry attempt {state.retry_count} failed: {exc}",
                success=False,
            )

    def unquarantine(self, job_name: str) -> bool:
        """Manually unquarantine a job. Returns True if it was quarantined."""
        state = self._jobs.get(job_name)
        if state is None or state.quarantined_at is None:
            return False
        state.quarantined_at = None
        state.quarantine_reason = ""
        state.retry_count = 0
        logger.info("Manually unquarantined job: %s", job_name)
        return True

    def is_quarantined(self, job_name: str) -> bool:
        """Check if a job is currently quarantined."""
        state = self._jobs.get(job_name)
        if state is None or state.quarantined_at is None:
            return False
        # Check auto-unquarantine.
        elapsed = (
            datetime.now(UTC) - datetime.fromisoformat(state.quarantined_at)
        ).total_seconds()
        return elapsed < state.auto_unquarantine_s

    def list_registered(self) -> list[str]:
        """List all registered job names."""
        return list(self._jobs.keys())
