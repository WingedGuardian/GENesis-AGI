"""Job health tracking methods for GenesisRuntime."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from genesis.runtime._core import GenesisRuntime

logger = logging.getLogger("genesis.runtime")


def record_job_success(rt: GenesisRuntime, job_name: str) -> None:
    """Record a successful scheduled job execution (in-memory + DB)."""
    now = datetime.now(UTC).isoformat()
    entry = rt._job_health.setdefault(job_name, {"consecutive_failures": 0})
    entry["last_run"] = now
    entry["last_success"] = now
    entry["consecutive_failures"] = 0
    rt._persist_job_health(job_name, entry, now)


def record_job_failure(rt: GenesisRuntime, job_name: str, error: str) -> None:
    """Record a failed scheduled job execution (in-memory + DB).

    When consecutive failures reach the retry threshold (3), triggers
    an automatic retry via the JobRetryRegistry if one is wired.
    """
    now = datetime.now(UTC).isoformat()
    entry = rt._job_health.setdefault(job_name, {"consecutive_failures": 0})
    entry["last_run"] = now
    entry["last_failure"] = now
    entry["last_error"] = error
    entry["consecutive_failures"] = entry.get("consecutive_failures", 0) + 1
    rt._persist_job_health(job_name, entry, now)

    consecutive = entry["consecutive_failures"]
    if consecutive >= 3 and rt._job_retry_registry is not None:
        from genesis.util.tasks import tracked_task

        tracked_task(
            rt._job_retry_registry.attempt_retry(job_name),
            name=f"job_retry:{job_name}",
        )


def register_channel(
    rt: GenesisRuntime, name: str, adapter: object, *, recipient: str | None = None
) -> None:
    """Register a channel adapter for outreach delivery."""
    if rt._outreach_pipeline is not None:
        rt._outreach_pipeline._channels[name] = adapter
        if recipient:
            rt._outreach_pipeline._recipients[name] = recipient
    if (rt._outreach_scheduler is not None
            and not rt._outreach_scheduler.is_running):
        logger.info("First outreach channel '%s' registered — starting scheduler", name)
        rt._outreach_scheduler.start()
