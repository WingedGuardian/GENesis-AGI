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

from genesis.ego.session import BudgetExceededError, CycleBlockedError
from genesis.ego.types import EgoConfig
from genesis.env import user_timezone

if TYPE_CHECKING:
    import aiosqlite

    from genesis.ego.session import EgoSession
    from genesis.observability.events import GenesisEventBus
    from genesis.surplus.idle_detector import IdleDetector

logger = logging.getLogger(__name__)

# User-recency tiers: (elapsed_threshold, max_interval_minutes).
# When the user hasn't had a foreground session in a while, the ego
# naturally winds down — through the cadence system, not self-suppression.
_RECENCY_TIERS: list[tuple[timedelta | None, int]] = [
    (timedelta(hours=24), 240),  # <24h:   current max (~6x/day)
    (timedelta(days=3), 480),  # 1-3d:   ~3x/day
    (timedelta(days=7), 1440),  # 3-7d:   ~1x/day
    (timedelta(days=14), 2880),  # 7-14d:  every other day
    (None, 4320),  # 14+d:   every 3 days
]


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

        # Deep-think counter: every Nth proactive cycle uses Opus instead
        # of the ego's base model. Only meaningful for egos that run Sonnet
        # by default (Genesis ego). User ego already runs Opus proactive.
        self._deep_think_interval = 5
        self._proactive_cycle_count = 0

        # Reactive event queue: events push here, debounce loop drains
        self._reactive_queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=100)
        self._reactive_task: asyncio.Task | None = None
        self._reactive_debounce_s = 300  # 5 minutes
        self._reactive_max_per_hour = 3
        self._reactive_timestamps: list[datetime] = []

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
        if self._config.morning_report_enabled:
            tz = user_timezone()
            self._scheduler.add_job(
                self._on_morning_report,
                CronTrigger(
                    hour=self._config.morning_report_hour,
                    minute=self._config.morning_report_minute,
                    timezone=tz,
                ),
                id="ego_morning_report",
                max_instances=1,
                misfire_grace_time=600,
            )
        # Mechanical sweep: expire stale proposals then dispatch approved
        # proposals every 30 min, independent of ego LLM cycles.
        self._scheduler.add_job(
            self._sweep_with_expiry,
            IntervalTrigger(minutes=30),
            id="ego_sweep_approved",
            max_instances=1,
            misfire_grace_time=300,
        )
        self._scheduler.start()
        self._reactive_task = asyncio.create_task(
            self._reactive_loop(), name=f"ego_reactive_{id(self)}",
        )
        self._running = True
        morning_str = (
            f", morning={self._config.morning_report_hour:02d}:"
            f"{self._config.morning_report_minute:02d} "
            f"{user_timezone()}"
            if self._config.morning_report_enabled
            else ", morning=disabled"
        )
        logger.info(
            "Ego cadence started (interval=%dm%s, reactive=enabled)",
            self._current_interval,
            morning_str,
        )

    async def stop(self) -> None:
        """Shut down APScheduler and reactive loop."""
        if self._reactive_task is not None:
            self._reactive_task.cancel()
            self._reactive_task = None
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

    # -- Reactive event queue -----------------------------------------------

    def push_reactive_event(self, event: dict) -> None:
        """Push an event that may trigger a reactive ego cycle.

        Events are debounced: the reactive loop waits 5 minutes after the
        first event before running a cycle (batching concurrent events).
        Rate-limited to 3 reactive cycles per hour.

        event keys: {"type": str, "summary": str, "priority": str?, "source": str?}
        """
        if not self._running or self._paused:
            return
        try:
            self._reactive_queue.put_nowait(event)
            logger.debug("Reactive event queued: %s", event.get("type", "?"))
        except asyncio.QueueFull:
            logger.warning("Reactive queue full — dropping event")

    async def _reactive_loop(self) -> None:
        """Background task: drain reactive queue with debounce, run cycle."""
        while True:
            try:
                # Block until first event arrives
                first_event = await self._reactive_queue.get()
                events = [first_event]

                # Debounce: wait, then drain anything that arrived meanwhile
                await asyncio.sleep(self._reactive_debounce_s)
                while not self._reactive_queue.empty():
                    try:
                        events.append(self._reactive_queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break

                # Rate limit: max N reactive cycles per hour
                now = datetime.now(UTC)
                cutoff = now - timedelta(hours=1)
                self._reactive_timestamps = [
                    ts for ts in self._reactive_timestamps if ts > cutoff
                ]
                if len(self._reactive_timestamps) >= self._reactive_max_per_hour:
                    logger.info(
                        "Reactive rate limit: %d/%d in last hour, skipping %d event(s)",
                        len(self._reactive_timestamps),
                        self._reactive_max_per_hour,
                        len(events),
                    )
                    continue

                # Run reactive cycle
                event_summary = "; ".join(
                    e.get("summary", e.get("type", "?"))[:80] for e in events[:5]
                )
                logger.info(
                    "Reactive cycle triggered by %d event(s): %s",
                    len(events),
                    event_summary,
                )

                async with self._lock:
                    if not self._should_run(skip_idle_check=True):
                        continue

                    try:
                        from genesis.ego.types import CycleType

                        cycle = await self._session.run_cycle(
                            cycle_type=CycleType.REACTIVE,
                        )
                    except (BudgetExceededError, CycleBlockedError) as exc:
                        logger.info("Reactive cycle gated: %s", exc)
                        continue
                    except Exception:
                        logger.error("Reactive cycle failed", exc_info=True)
                        self._record_failure("reactive cycle failed")
                        continue

                    if cycle is not None:
                        self._record_success()
                        self._reactive_timestamps.append(datetime.now(UTC))
                        logger.info(
                            "Reactive cycle %s completed (cost=$%.4f)",
                            cycle.id,
                            cycle.cost_usd,
                        )

            except asyncio.CancelledError:
                logger.debug("Reactive loop cancelled")
                return
            except Exception:
                logger.error("Reactive loop error", exc_info=True)
                await asyncio.sleep(60)  # Back off on unexpected errors

    # -- Sweep helpers -----------------------------------------------------

    async def _sweep_with_expiry(self) -> None:
        """Expire stale proposals, dispatch approved, check deadlines."""
        try:
            from genesis.db.crud import ego as ego_crud

            expired = await ego_crud.expire_stale_proposals(self._session._db)
            if expired:
                logger.info("Pre-sweep expiry: %d proposal(s) expired", expired)
        except Exception:
            logger.warning("Pre-sweep expiry failed", exc_info=True)

        await self._session.sweep_approved_proposals()

        # Deadline scanner: push reactive events for approaching deadlines
        await self._check_approaching_deadlines()

    async def _check_approaching_deadlines(self) -> None:
        """Push reactive events for events approaching within 48h."""
        try:
            from genesis.db.crud import memory_events

            events = await memory_events.approaching_deadlines(
                self._session._db, days=2, limit=5,
            )
            if not events:
                return

            for evt in events:
                subj = evt.get("subject", "?")
                verb = evt.get("verb", "?")
                obj = evt.get("object", "")
                date = evt.get("event_date", "")[:10]
                self.push_reactive_event({
                    "type": "deadline_approaching",
                    "summary": f"{subj} {verb} {obj} on {date}",
                    "priority": "high",
                    "source": "deadline_scanner",
                })
            logger.debug("Deadline scanner found %d approaching event(s)", len(events))
        except Exception:
            logger.debug("Deadline scanner failed", exc_info=True)

    # -- Tick handlers -----------------------------------------------------

    async def _on_tick(self) -> None:
        """Interval trigger handler. Checks all gates then runs cycle."""
        self._emit_heartbeat("tick")
        async with self._lock:
            if not self._should_run(skip_idle_check=False):
                return

            # Deep-think: every Nth proactive cycle upgrades to Opus.
            # Only effective for egos that normally run Sonnet (Genesis ego).
            self._proactive_cycle_count += 1
            model_override = None
            if (
                self._deep_think_interval > 0
                and self._proactive_cycle_count % self._deep_think_interval == 0
                and self._config.model != "opus"
            ):
                model_override = "opus"
                logger.info(
                    "Deep-think cycle %d — upgrading to Opus",
                    self._proactive_cycle_count,
                )

            try:
                cycle = await self._session.run_cycle(
                    model_override=model_override,
                )
            except BudgetExceededError:
                # Budget exhaustion is intentional, not a failure.
                # Don't trip the circuit breaker.
                logger.info("Ego cycle skipped — budget exceeded")
                return
            except CycleBlockedError as exc:
                # Approval gate is a gate, not a failure.
                # Don't trip the circuit breaker.
                logger.info("Ego cycle gated: %s", exc)
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
            await self._update_interval(had_proposals=bool(proposals))

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
            except CycleBlockedError as exc:
                logger.info("Ego morning report gated: %s", exc)
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
            await self._update_interval(had_proposals=True)

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
                "Ego circuit breaker OPEN — %d consecutive failures, pausing for %d minutes",
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

    async def _recency_max_interval(self) -> int:
        """Dynamic max_interval based on last foreground session.

        Returns a ceiling that adapts to how recently the user was active.
        Falls back to the static config max if no foreground data is
        available.
        """
        try:
            cursor = await self._db.execute(
                "SELECT last_activity_at FROM cc_sessions "
                "WHERE source_tag = 'foreground' "
                "AND status IN ('active', 'completed', 'checkpointed') "
                "ORDER BY last_activity_at DESC LIMIT 1",
            )
            row = await cursor.fetchone()
        except Exception:
            return self._config.max_interval_minutes

        if not row or not row[0]:
            return self._config.max_interval_minutes

        try:
            last_active = datetime.fromisoformat(row[0])
            if last_active.tzinfo is None:
                last_active = last_active.replace(tzinfo=UTC)
        except (ValueError, TypeError):
            return self._config.max_interval_minutes

        elapsed = datetime.now(UTC) - last_active

        for threshold, max_mins in _RECENCY_TIERS:
            if threshold is None or elapsed < threshold:
                return max_mins

        return self._config.max_interval_minutes

    async def _update_interval(self, *, had_proposals: bool) -> None:
        """Adjust cycle interval based on productivity and user recency.

        - Idle cycle (no proposals): multiply by backoff_multiplier
        - Productive cycle: reset to base cadence_minutes
        - Max interval adapts to user recency (longer absence → higher cap)
        """
        recency_max = await self._recency_max_interval()

        if had_proposals:
            new_interval = self._config.cadence_minutes
        else:
            new_interval = min(
                int(self._current_interval * self._config.backoff_multiplier),
                recency_max,
            )

        if new_interval != self._current_interval:
            old_interval = self._current_interval
            self._current_interval = new_interval
            try:
                self._scheduler.reschedule_job(
                    "ego_cycle",
                    trigger=IntervalTrigger(minutes=new_interval),
                )
                logger.info(
                    "Ego interval adjusted: %dm → %dm (recency_max=%dm)",
                    old_interval,
                    new_interval,
                    recency_max,
                )
            except Exception:
                logger.warning(
                    "Failed to reschedule ego interval",
                    exc_info=True,
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
