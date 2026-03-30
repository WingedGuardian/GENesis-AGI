"""Ego cadence manager — controls when the ego runs.

Owns an APScheduler with two jobs:
1. IntervalTrigger for regular cycles (adaptive backoff)
2. CronTrigger for the mandatory morning report
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from genesis.ego.session import BudgetExceededError
from genesis.ego.types import EgoConfig

if TYPE_CHECKING:
    import aiosqlite

    from genesis.ego.session import EgoSession
    from genesis.observability.events import GenesisEventBus
    from genesis.surplus.idle_detector import IdleDetector

logger = logging.getLogger(__name__)


class EgoCadenceManager:
    """Manages when the ego runs.

    Responsibilities:
    - APScheduler job registration (interval + morning cron)
    - Activity detection gate (IdleDetector)
    - Adaptive backoff (double interval on idle cycles, reset on proposals)
    - Circuit breaker (N consecutive failures -> pause)
    - Pause/resume controls

    Does NOT own the EgoSession — just calls ``session.run_cycle()``.
    """

    def __init__(
        self,
        *,
        session: EgoSession,
        config: EgoConfig,
        idle_detector: IdleDetector | None = None,
        db: aiosqlite.Connection,
        event_bus: GenesisEventBus | None = None,
    ) -> None:
        self._session = session
        self._config = config
        self._idle_detector = idle_detector
        self._db = db
        self._event_bus = event_bus

        self._scheduler = AsyncIOScheduler()
        self._paused = False
        self._running = False

        # Circuit breaker state
        self._consecutive_failures = 0
        self._circuit_open_until: datetime | None = None

        # Adaptive interval
        self._current_interval = config.cadence_minutes

        # Prevent concurrent cycles (interval + morning could overlap)
        self._lock = asyncio.Lock()

    # -- Lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Register APScheduler jobs and start."""
        self._scheduler.add_job(
            self._on_tick,
            IntervalTrigger(minutes=self._current_interval),
            id="ego_cycle",
            max_instances=1,
            misfire_grace_time=300,
        )
        self._scheduler.add_job(
            self._on_morning_report,
            CronTrigger(
                hour=self._config.morning_report_hour,
                minute=self._config.morning_report_minute,
                timezone=self._config.morning_report_timezone,
            ),
            id="ego_morning_report",
            max_instances=1,
            misfire_grace_time=600,
        )
        self._scheduler.start()
        self._running = True
        logger.info(
            "Ego cadence started (interval=%dm, morning=%02d:%02d %s)",
            self._current_interval,
            self._config.morning_report_hour,
            self._config.morning_report_minute,
            self._config.morning_report_timezone,
        )

    async def stop(self) -> None:
        """Shut down APScheduler."""
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)
        self._running = False
        logger.info("Ego cadence stopped")

    def pause(self) -> None:
        """Pause ego cycles (manual control)."""
        self._paused = True
        logger.info("Ego paused")

    def resume(self) -> None:
        """Resume ego cycles."""
        self._paused = False
        logger.info("Ego resumed")

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def is_paused(self) -> bool:
        return self._paused

    @property
    def current_interval_minutes(self) -> int:
        return self._current_interval

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    # -- Tick handlers -----------------------------------------------------

    async def _on_tick(self) -> None:
        """Interval trigger handler. Checks all gates then runs cycle."""
        self._emit_heartbeat("tick")
        async with self._lock:
            if not self._should_run(skip_idle_check=False):
                return

            try:
                cycle = await self._session.run_cycle()
            except BudgetExceededError:
                # Budget exhaustion is intentional, not a failure.
                # Don't trip the circuit breaker.
                logger.info("Ego cycle skipped — budget exceeded")
                return
            except Exception as exc:
                logger.error("Ego cycle failed with exception", exc_info=True)
                self._record_failure(str(exc))
                return

            if cycle is None:
                # CC error or session creation failure
                self._record_failure("cycle returned None")
                return

            # Check if the cycle produced meaningful output.
            # A parse-failure cycle has empty focus_summary — treat as soft failure
            # so the circuit breaker can detect persistent LLM issues.
            proposals = []
            try:
                proposals = json.loads(cycle.proposals_json)
            except (json.JSONDecodeError, TypeError):
                logger.debug("Could not parse proposals_json for cycle %s", cycle.id)

            if not cycle.focus_summary and not proposals:
                logger.warning("Ego cycle %s produced no usable output", cycle.id)
                self._record_failure("cycle produced no usable output")
                return

            self._record_success()
            self._update_interval(had_proposals=bool(proposals))

    async def _on_morning_report(self) -> None:
        """Cron trigger handler. Morning report cycle (skips idle check)."""
        async with self._lock:
            if not self._should_run(skip_idle_check=True):
                return

            try:
                cycle = await self._session.run_cycle(is_morning_report=True)
            except BudgetExceededError:
                logger.info("Ego morning report skipped — budget exceeded")
                return
            except Exception as exc:
                logger.error("Ego morning report failed", exc_info=True)
                self._record_failure(str(exc))
                return

            if cycle is None:
                self._record_failure("morning report returned None")
                return

            self._record_success()
            # Morning report always resets interval to base
            self._update_interval(had_proposals=True)

    # -- Gate logic --------------------------------------------------------

    def _should_run(self, *, skip_idle_check: bool = False) -> bool:
        """Check all gates. Returns True if the cycle should proceed."""
        # Don't run before onboarding completes
        setup_marker = Path.home() / ".genesis" / "setup-complete"
        if not setup_marker.exists():
            logger.debug("Ego cycle skipped — onboarding not complete")
            return False

        if self._paused:
            logger.debug("Ego cycle skipped — paused")
            return False

        # Check global Genesis pause
        try:
            from genesis.runtime import GenesisRuntime
            if GenesisRuntime.instance().paused:
                logger.debug("Ego cycle skipped — Genesis paused")
                return False
        except ImportError:
            pass  # Runtime not available (testing, standalone)
        except Exception:
            logger.debug("Runtime pause check failed", exc_info=True)

        if self._is_circuit_open():
            logger.debug("Ego cycle skipped — circuit breaker open")
            return False

        if (
            not skip_idle_check
            and self._idle_detector is not None
            and not self._idle_detector.is_idle(
                threshold_minutes=self._config.activity_threshold_minutes,
            )
        ):
            logger.debug("Ego cycle skipped — user active")
            return False

        return True

    # -- Circuit breaker ---------------------------------------------------

    def _is_circuit_open(self) -> bool:
        """True if circuit breaker is tripped and hasn't expired."""
        if self._circuit_open_until is None:
            return False
        if datetime.now(UTC) >= self._circuit_open_until:
            # Circuit expired — close it
            self._circuit_open_until = None
            self._consecutive_failures = 0
            logger.info("Ego circuit breaker expired — closing")
            return False
        return True

    def _record_success(self) -> None:
        """Reset circuit breaker, record job success."""
        self._consecutive_failures = 0
        self._circuit_open_until = None

        try:
            from genesis.runtime import GenesisRuntime
            GenesisRuntime.instance().record_job_success("ego_cycle")
        except ImportError:
            pass
        except Exception:
            logger.debug("Failed to record ego job success", exc_info=True)

    def _record_failure(self, error: str) -> None:
        """Increment circuit breaker, record job failure."""
        self._consecutive_failures += 1

        if self._consecutive_failures >= self._config.consecutive_failure_limit:
            self._circuit_open_until = datetime.now(UTC) + timedelta(
                minutes=self._config.failure_backoff_minutes,
            )
            logger.warning(
                "Ego circuit breaker OPEN — %d consecutive failures, "
                "pausing for %d minutes",
                self._consecutive_failures,
                self._config.failure_backoff_minutes,
            )

        try:
            from genesis.runtime import GenesisRuntime
            GenesisRuntime.instance().record_job_failure("ego_cycle", error)
        except ImportError:
            pass
        except Exception:
            logger.debug("Failed to record ego job failure", exc_info=True)

    # -- Adaptive interval -------------------------------------------------

    def _update_interval(self, *, had_proposals: bool) -> None:
        """Adjust cycle interval based on productivity.

        - Idle cycle (no proposals): multiply by backoff_multiplier
        - Productive cycle: reset to base cadence_minutes
        - Never exceed max_interval_minutes
        """
        if had_proposals:
            new_interval = self._config.cadence_minutes
        else:
            new_interval = min(
                int(self._current_interval * self._config.backoff_multiplier),
                self._config.max_interval_minutes,
            )

        if new_interval != self._current_interval:
            self._current_interval = new_interval
            try:
                self._scheduler.reschedule_job(
                    "ego_cycle",
                    trigger=IntervalTrigger(minutes=new_interval),
                )
                logger.info("Ego interval adjusted to %d minutes", new_interval)
            except Exception:
                logger.warning(
                    "Failed to reschedule ego interval", exc_info=True,
                )

    # -- Observability -----------------------------------------------------

    def _emit_heartbeat(self, trigger: str) -> None:
        """Emit a DEBUG heartbeat event for the neural monitor."""
        if self._event_bus is None:
            return
        try:
            from genesis.observability.types import Severity, Subsystem
            from genesis.util.tasks import tracked_task

            tracked_task(
                self._event_bus.emit(
                    Subsystem.EGO,
                    Severity.DEBUG,
                    "heartbeat",
                    f"ego_{trigger} (interval={self._current_interval}m, "
                    f"failures={self._consecutive_failures})",
                ),
                name="ego_heartbeat",
            )
        except Exception:
            pass  # Heartbeat emission is best-effort
