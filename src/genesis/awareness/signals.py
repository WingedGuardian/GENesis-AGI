"""Signal collectors for the Awareness Loop.

Each collector reads one signal source and returns a normalized 0.0–1.0 value.

This module defines the SignalCollector protocol and base-layer stub/utility
collectors (e.g. ContainerMemoryCollector, StrategicTimerCollector).  At startup,
runtime._init_learning() replaces the default stubs with real implementations
from genesis.learning.signals.* via awareness_loop.replace_collectors().
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from genesis.awareness.types import SignalReading
from genesis.routing.types import ProviderState

if TYPE_CHECKING:
    from genesis.routing.circuit_breaker import CircuitBreakerRegistry

logger = logging.getLogger(__name__)


@runtime_checkable
class SignalCollector(Protocol):
    """Protocol for signal collectors."""

    signal_name: str

    async def collect(self) -> SignalReading: ...


def _stub_reading(name: str, source: str) -> SignalReading:
    """Create a zero-value reading for Phase 1 stubs."""
    return SignalReading(
        name=name, value=0.0, source=source,
        collected_at=datetime.now(UTC).isoformat(),
    )


class ConversationCollector:
    signal_name = "conversations_since_reflection"

    async def collect(self) -> SignalReading:
        return _stub_reading(self.signal_name, "agent_zero")


class TaskQualityCollector:
    signal_name = "task_completion_quality"

    async def collect(self) -> SignalReading:
        return _stub_reading(self.signal_name, "agent_zero")


class OutreachEngagementCollector:
    signal_name = "outreach_engagement_data"

    async def collect(self) -> SignalReading:
        return _stub_reading(self.signal_name, "outreach_mcp")


class ReconFindingsCollector:
    signal_name = "recon_findings_pending"

    async def collect(self) -> SignalReading:
        return _stub_reading(self.signal_name, "recon_mcp")


class MemoryBacklogCollector:
    signal_name = "unprocessed_memory_backlog"

    async def collect(self) -> SignalReading:
        return _stub_reading(self.signal_name, "memory_mcp")


class BudgetCollector:
    signal_name = "budget_pct_consumed"

    async def collect(self) -> SignalReading:
        return _stub_reading(self.signal_name, "health_mcp")


class ErrorSpikeCollector:
    signal_name = "software_error_spike"

    def __init__(self, *, registry: CircuitBreakerRegistry | None = None) -> None:
        self._registry = registry

    async def collect(self) -> SignalReading:
        if not self._registry or not self._registry._breakers:
            return _stub_reading(self.signal_name, "health_mcp")
        total = len(self._registry._breakers)
        open_count = sum(
            1 for cb in self._registry._breakers.values()
            if cb.state == ProviderState.OPEN
        )
        value = open_count / total if total > 0 else 0.0
        return SignalReading(
            name=self.signal_name, value=value, source="health_mcp",
            collected_at=datetime.now(UTC).isoformat(),
        )


class CriticalFailureCollector:
    signal_name = "critical_failure"

    def __init__(self, *, registry: CircuitBreakerRegistry | None = None) -> None:
        self._registry = registry

    async def collect(self) -> SignalReading:
        if not self._registry or not self._registry._breakers:
            return _stub_reading(self.signal_name, "health_mcp")
        cloud_names = [
            name for name, cfg in self._registry._providers.items()
            if cfg.provider_type != "ollama"
        ]
        if not cloud_names:
            return _stub_reading(self.signal_name, "health_mcp")
        all_open = all(
            self._registry.get(name).state == ProviderState.OPEN
            for name in cloud_names
        )
        value = 1.0 if all_open else 0.0
        return SignalReading(
            name=self.signal_name, value=value, source="health_mcp",
            collected_at=datetime.now(UTC).isoformat(),
        )


class StrategicTimerCollector:
    """Reports normalized time since last strategic reflection.

    Queries awareness_ticks for last Strategic tick. Normalizes elapsed
    time: 0d = 0.0, 5d = 0.5, 10d = 0.75, 15d+ = 1.0.
    """

    signal_name = "time_since_last_strategic"

    def __init__(self, db=None) -> None:
        self._db = db

    async def collect(self) -> SignalReading:
        if self._db is None:
            return _stub_reading(self.signal_name, "clock")

        from genesis.db.crud import awareness_ticks as at_crud

        last = await at_crud.last_at_depth(self._db, "Strategic")
        if last is None:
            # Never had a strategic reflection — maximally overdue
            value = 1.0
        else:
            last_dt = datetime.fromisoformat(last["created_at"])
            elapsed = (datetime.now(UTC) - last_dt).total_seconds()
            # Normalize: 5d (432000s) = 0.5, 15d (1296000s) = 1.0
            value = min(1.0, elapsed / 1296000)

        return SignalReading(
            name=self.signal_name,
            value=round(value, 3),
            source="clock",
            collected_at=datetime.now(UTC).isoformat(),
        )


class ContainerMemoryCollector:
    """Reports container memory usage as a percentage of cgroup limit.

    Reads /sys/fs/cgroup/memory.current and memory.max. Returns 0.0–1.0
    where 1.0 = at the cgroup limit. Returns 0.0 if cgroup info unavailable.
    """

    signal_name = "container_memory_pct"

    async def collect(self) -> SignalReading:
        from genesis.autonomy.watchdog import get_container_memory

        mem = get_container_memory()
        if mem is None or mem[1] == 0:
            return _stub_reading(self.signal_name, "cgroup")

        current, limit = mem
        pct = current / limit  # 0.0–1.0
        return SignalReading(
            name=self.signal_name,
            value=round(pct, 3),
            source="cgroup",
            collected_at=datetime.now(UTC).isoformat(),
            normal_max=0.80,
            warning_threshold=0.85,
            critical_threshold=0.90,
        )


class JobHealthCollector:
    """Reports normalized job health based on consecutive failures.

    Reads runtime.job_health for scheduled jobs with consecutive_failures > 0.
    Value = max(consecutive_failures) / threshold, clamped to 1.0.
    Metadata includes list of failed job names and quarantine status.
    """

    signal_name = "scheduled_job_health"

    def __init__(self, *, runtime=None, failure_threshold: int = 2) -> None:
        self._runtime = runtime
        self._threshold = failure_threshold

    async def collect(self) -> SignalReading:
        if self._runtime is None:
            return _stub_reading(self.signal_name, "runtime")

        job_health = self._runtime.job_health
        if not job_health:
            return SignalReading(
                name=self.signal_name, value=0.0, source="runtime",
                collected_at=datetime.now(UTC).isoformat(),
            )

        failed_jobs = {
            name: info
            for name, info in job_health.items()
            if info.get("consecutive_failures", 0) >= self._threshold
        }

        if not failed_jobs:
            return SignalReading(
                name=self.signal_name, value=0.0, source="runtime",
                collected_at=datetime.now(UTC).isoformat(),
            )

        max_failures = max(
            info.get("consecutive_failures", 0) for info in failed_jobs.values()
        )
        # Normalize: threshold = 0.5, 2x threshold = 1.0
        value = min(1.0, max_failures / (self._threshold * 2))

        return SignalReading(
            name=self.signal_name,
            value=round(value, 3),
            source="runtime",
            collected_at=datetime.now(UTC).isoformat(),
        )


async def collect_all(collectors: list) -> list[SignalReading]:
    """Run all collectors concurrently. Failures return 0.0, never propagate."""

    async def _safe_collect(c) -> SignalReading:
        try:
            return await c.collect()
        except Exception:
            logger.error("Signal collector %s failed", c.signal_name, exc_info=True)
            return SignalReading(
                name=c.signal_name, value=0.0, source="error",
                collected_at=datetime.now(UTC).isoformat(),
                failed=True,
            )

    return list(await asyncio.gather(*[_safe_collect(c) for c in collectors]))
