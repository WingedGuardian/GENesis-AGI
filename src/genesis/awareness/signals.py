"""Signal collectors for the Awareness Loop.

Each collector reads one signal source and returns a normalized 0.0–1.0 value.

==============================================================================
⚠️  IMPORTANT — READ BEFORE DELETING ANYTHING HERE  ⚠️

This module is the awareness loop's bootstrap-layer collector surface. It is
loaded by ``runtime/init/awareness.py`` *before* ``runtime/init/learning.py``
runs.  Several classes below are deliberate **bootstrap placeholders**
(return ``value=0.0`` via ``_bootstrap_placeholder_reading``) that are
**replaced at runtime** by the real implementations in
``genesis.learning.signals.*`` via ``awareness_loop.replace_collectors()``.

A placeholder class with a trivial ``collect()`` body is **NOT dead code**.
It is part of the two-phase bootstrap contract: awareness starts with these
placeholders so the loop can begin ticking immediately, then the learning
init swap wires the real collectors in once their dependencies exist.

Deleting a placeholder class without also removing its import+registration
in ``runtime/init/awareness.py`` and its swap target in
``runtime/init/learning.py`` will break bootstrap.

Tagged with ``GROUNDWORK(signal-bootstrap)`` below for explicit protection.
==============================================================================

Note: Some collectors in this file are **real** implementations
(``ErrorSpikeCollector``, ``CriticalFailureCollector``,
``StrategicTimerCollector``, ``ContainerMemoryCollector``,
``JobHealthCollector``). They use ``_bootstrap_placeholder_reading`` only
as a graceful fallback when their own dependencies (registry, cgroup,
runtime handle) are unavailable — not as their primary path.
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


def _bootstrap_placeholder_reading(name: str, source: str) -> SignalReading:
    """Create a zero-value signal reading.

    Two distinct uses (both intentional — see module docstring):

    1. **Pre-swap bootstrap placeholder** — used by the pure-placeholder
       collectors below (``ConversationCollector``, ``TaskQualityCollector``,
       etc.). These exist only so the awareness loop can boot; they are
       replaced at runtime by real implementations from
       ``genesis.learning.signals.*``.
    2. **Graceful fallback in real collectors** — used by
       ``ErrorSpikeCollector``, ``CriticalFailureCollector``,
       ``StrategicTimerCollector``, etc. when their dependencies (registry,
       runtime handle, cgroup) aren't available yet.

    A zero reading means "this signal cannot be measured right now," not
    "this signal is dead code." Do NOT delete collectors that call this
    function without first tracing their registration and swap sites.
    """
    return SignalReading(
        name=name, value=0.0, source=source,
        collected_at=datetime.now(UTC).isoformat(),
    )


# GROUNDWORK(signal-bootstrap): pre-swap placeholder. Replaced at runtime
# by genesis.learning.signals.conversation.ConversationCollector via
# awareness_loop.replace_collectors() in runtime/init/learning.py. Do not
# delete without updating both bootstrap sites.
class ConversationCollector:
    signal_name = "conversations_since_reflection"

    async def collect(self) -> SignalReading:
        return _bootstrap_placeholder_reading(self.signal_name, "genesis")


# GROUNDWORK(signal-bootstrap): pre-swap placeholder. Replaced at runtime
# by genesis.learning.signals.task_quality.TaskQualityCollector.
class TaskQualityCollector:
    signal_name = "task_completion_quality"

    async def collect(self) -> SignalReading:
        return _bootstrap_placeholder_reading(self.signal_name, "genesis")


# GROUNDWORK(signal-bootstrap): pre-swap placeholder. Replaced at runtime
# by genesis.learning.signals.outreach_engagement.OutreachEngagementCollector.
class OutreachEngagementCollector:
    signal_name = "outreach_engagement_data"

    async def collect(self) -> SignalReading:
        return _bootstrap_placeholder_reading(self.signal_name, "outreach_mcp")


# GROUNDWORK(signal-bootstrap): pre-swap placeholder. Replaced at runtime
# by genesis.learning.signals.recon_findings.ReconFindingsCollector.
class ReconFindingsCollector:
    signal_name = "recon_findings_pending"

    async def collect(self) -> SignalReading:
        return _bootstrap_placeholder_reading(self.signal_name, "recon_mcp")


# GROUNDWORK(signal-bootstrap): pre-swap placeholder. Replaced at runtime
# by genesis.learning.signals.budget.BudgetCollector.
class BudgetCollector:
    signal_name = "budget_pct_consumed"

    async def collect(self) -> SignalReading:
        return _bootstrap_placeholder_reading(self.signal_name, "health_mcp")


# GROUNDWORK(signal-bootstrap): pre-swap placeholder. Replaced at runtime
# by genesis.learning.signals.light_cascade.LightCascadeCollector.
class LightCascadeCollector:
    signal_name = "light_count_since_deep"

    async def collect(self) -> SignalReading:
        return _bootstrap_placeholder_reading(self.signal_name, "awareness_loop")


# GROUNDWORK(signal-bootstrap): pre-swap placeholder. Replaced at runtime
# by genesis.learning.signals.sentinel_activity.SentinelActivityCollector.
class SentinelActivityCollector:
    signal_name = "sentinel_activity"

    async def collect(self) -> SignalReading:
        return _bootstrap_placeholder_reading(self.signal_name, "sentinel")


# GROUNDWORK(signal-bootstrap): pre-swap placeholder. Replaced at runtime
# by genesis.learning.signals.guardian_activity.GuardianActivityCollector.
class GuardianActivityCollector:
    signal_name = "guardian_activity"

    async def collect(self) -> SignalReading:
        return _bootstrap_placeholder_reading(self.signal_name, "guardian")


# GROUNDWORK(signal-bootstrap): pre-swap placeholder. Replaced at runtime
# by genesis.learning.signals.surplus_activity.SurplusActivityCollector.
class SurplusActivityCollector:
    signal_name = "surplus_activity"

    async def collect(self) -> SignalReading:
        return _bootstrap_placeholder_reading(self.signal_name, "surplus")


# GROUNDWORK(signal-bootstrap): pre-swap placeholder. Replaced at runtime
# by genesis.learning.signals.autonomy_activity.AutonomyActivityCollector.
class AutonomyActivityCollector:
    signal_name = "autonomy_activity"

    async def collect(self) -> SignalReading:
        return _bootstrap_placeholder_reading(self.signal_name, "autonomy")


# GROUNDWORK(signal-bootstrap): pre-swap placeholder. Replaced at runtime
# by genesis.learning.signals.user_goal_staleness.UserGoalStalenessCollector.
class UserGoalStalenessCollector:
    signal_name = "user_goal_staleness"

    async def collect(self) -> SignalReading:
        return _bootstrap_placeholder_reading(self.signal_name, "follow_ups+user_model")


# GROUNDWORK(signal-bootstrap): pre-swap placeholder. Replaced at runtime
# by genesis.learning.signals.user_session_pattern.UserSessionPatternCollector.
class UserSessionPatternCollector:
    signal_name = "user_session_pattern"

    async def collect(self) -> SignalReading:
        return _bootstrap_placeholder_reading(self.signal_name, "cc_sessions")


class ErrorSpikeCollector:
    signal_name = "software_error_spike"

    def __init__(self, *, registry: CircuitBreakerRegistry | None = None) -> None:
        self._registry = registry

    async def collect(self) -> SignalReading:
        if not self._registry or not self._registry._breakers:
            return _bootstrap_placeholder_reading(self.signal_name, "health_mcp")
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
            return _bootstrap_placeholder_reading(self.signal_name, "health_mcp")
        cloud_names = [
            name for name, cfg in self._registry._providers.items()
            if cfg.provider_type != "ollama"
        ]
        if not cloud_names:
            return _bootstrap_placeholder_reading(self.signal_name, "health_mcp")
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
            return _bootstrap_placeholder_reading(self.signal_name, "clock")

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
            baseline_note="Time since last strategic reflection. 0.0=recent, 1.0=15+ days overdue",
        )


class ContainerMemoryCollector:
    """Reports non-reclaimable container memory (anon+kernel) as fraction of limit.

    Reads anon and kernel from /sys/fs/cgroup/memory.stat + memory.max.
    Returns 0.0–1.0 where 1.0 = at the cgroup limit. This excludes
    reclaimable page cache, which inflates memory.current and causes
    false pressure signals.
    """

    signal_name = "container_memory_pct"

    async def collect(self) -> SignalReading:
        from genesis.autonomy.watchdog import get_container_anon_memory

        mem = get_container_anon_memory()
        if mem is None or mem[1] == 0:
            return _bootstrap_placeholder_reading(self.signal_name, "cgroup")

        anon_kernel, limit = mem
        pct = anon_kernel / limit  # 0.0–1.0
        return SignalReading(
            name=self.signal_name,
            value=round(pct, 3),
            source="cgroup",
            collected_at=datetime.now(UTC).isoformat(),
            normal_max=0.80,
            warning_threshold=0.85,
            critical_threshold=0.90,
            baseline_note="Includes page cache; 70-80% is normal for containerized workloads",
        )


class ProcessHealthCollector:
    """Reports count of browser-related processes as 0.0–1.0 signal.

    Uses pgrep to detect Camoufox (camoufox-bin), Chromium (ms-playwright chrome),
    and Playwright driver (node) processes. Non-zero means browser processes exist;
    high values (6+) indicate orphaned process accumulation.

    Patterns verified against actual ``/proc/PID/cmdline`` entries — they match
    only browser binaries, not the MCP server's Python process.
    """

    signal_name = "stale_browser_processes"

    async def collect(self) -> SignalReading:
        try:
            from genesis.browser.types import BROWSER_PGREP_PATTERNS

            count = 0
            for pattern in BROWSER_PGREP_PATTERNS:
                proc = await asyncio.create_subprocess_exec(
                    "pgrep", "-fc", pattern,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                stdout, _ = await proc.communicate()
                if proc.returncode == 0:
                    count += int(stdout.strip())
            # 0 procs = 0.0, 6+ procs = 1.0
            value = min(1.0, count / 6.0)
            return SignalReading(
                name=self.signal_name, value=value, source="process",
                collected_at=datetime.now(UTC).isoformat(),
                warning_threshold=0.3, critical_threshold=0.5,
                baseline_note="0=no browsers (normal idle). 1-2 during active browsing is expected",
            )
        except Exception:
            logger.error("ProcessHealthCollector failed", exc_info=True)
            return SignalReading(
                name=self.signal_name, value=0.0, source="process",
                collected_at=datetime.now(UTC).isoformat(), failed=True,
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
            return _bootstrap_placeholder_reading(self.signal_name, "runtime")

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
            baseline_note="0.0=all jobs healthy. Brief spikes normal after restart",
        )


class SchedulerLivenessCollector:
    """Detects zombie APScheduler instances (running=True but no jobs executing).

    Checks last job timestamps for the surplus scheduler.  If the surplus
    scheduler's most recent job is older than ``stale_threshold_s`` seconds,
    the signal fires (value > 0).  The awareness loop's own scheduler cannot
    be self-monitored — that's handled by the status.json heartbeat +
    external watchdog.
    """

    signal_name = "scheduler_liveness"

    def __init__(
        self,
        *,
        runtime=None,
        stale_threshold_s: int = 900,  # 15 min (surplus dispatch runs every 5m)
    ) -> None:
        self._runtime = runtime
        self._stale_threshold_s = stale_threshold_s

    async def collect(self) -> SignalReading:
        if self._runtime is None:
            return _bootstrap_placeholder_reading(self.signal_name, "runtime")

        now = datetime.now(UTC)
        stale_schedulers: list[str] = []

        # Check surplus scheduler liveness via job_health timestamps
        surplus_sched = getattr(self._runtime, "_surplus_scheduler", None)
        if surplus_sched is not None:
            # Look at surplus-specific jobs in job_health
            jh = self._runtime.job_health
            surplus_jobs = [
                "surplus_dispatch", "surplus_brainstorm",
                "schedule_code_audit", "schedule_code_index",
            ]
            latest_run: datetime | None = None
            for job_name in surplus_jobs:
                entry = jh.get(job_name, {})
                last_run_str = entry.get("last_run")
                if last_run_str:
                    try:
                        lr = datetime.fromisoformat(last_run_str)
                        if latest_run is None or lr > latest_run:
                            latest_run = lr
                    except (ValueError, TypeError):
                        pass

            if latest_run is not None:
                age_s = (now - latest_run).total_seconds()
                if age_s > self._stale_threshold_s:
                    stale_schedulers.append(
                        f"surplus (last job {int(age_s)}s ago)"
                    )

        if not stale_schedulers:
            return SignalReading(
                name=self.signal_name, value=0.0, source="runtime",
                collected_at=now.isoformat(),
                baseline_note="0.0=scheduler active. Rises if surplus jobs stop running",
            )

        return SignalReading(
            name=self.signal_name,
            value=min(1.0, len(stale_schedulers) * 0.5),
            source="runtime",
            collected_at=now.isoformat(),
            metadata={"stale": stale_schedulers},
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
