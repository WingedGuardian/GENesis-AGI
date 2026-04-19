"""AwarenessLoop — the system's heartbeat.

Orchestrates the tick pipeline: collect signals → score → classify → store.
APScheduler drives the 5-minute interval. perform_tick() is the testable core.

When running inside Agent Zero (later phases), the scheduler will be started
via DeferredTask, matching AZ's job_loop.py pattern. Phase 1 tests run standalone.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import aiosqlite
from apscheduler.events import EVENT_JOB_MAX_INSTANCES, EVENT_JOB_MISSED
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from genesis.awareness.classifier import classify_depth
from genesis.awareness.scorer import compute_scores, get_staleness_context
from genesis.awareness.signals import SignalCollector, collect_all
from genesis.awareness.types import Depth, TickResult
from genesis.cc.contingency import RATE_LIMIT_DEFERRAL_TTL_S
from genesis.db.crud import awareness_ticks, observations
from genesis.observability.events import GenesisEventBus
from genesis.observability.types import Severity, Subsystem
from genesis.resilience.state import CloudStatus
from genesis.routing.types import DegradationLevel

if TYPE_CHECKING:
    from genesis.routing.circuit_breaker import CircuitBreakerRegistry

logger = logging.getLogger(__name__)

# Maps circuit-breaker degradation levels to resilience cloud axis states.
_DEGRADATION_TO_CLOUD: dict[DegradationLevel, CloudStatus] = {
    DegradationLevel.NORMAL: CloudStatus.NORMAL,
    DegradationLevel.FALLBACK: CloudStatus.FALLBACK,
    DegradationLevel.REDUCED: CloudStatus.REDUCED,
    DegradationLevel.ESSENTIAL: CloudStatus.ESSENTIAL,
    DegradationLevel.MEMORY_IMPAIRED: CloudStatus.REDUCED,
    DegradationLevel.LOCAL_COMPUTE_DOWN: CloudStatus.OFFLINE,
}


async def perform_tick(
    db: aiosqlite.Connection,
    collectors: list,
    *,
    source: str = "scheduled",
    reason: str | None = None,
    reflection_engine=None,
    cc_reflection_bridge=None,
    deferred_queue=None,
    dispatch_reflection: bool = True,
) -> TickResult:
    """Execute one awareness tick. Testable without the scheduler."""
    now = datetime.now(UTC).isoformat()
    tick_id = str(uuid.uuid4())
    escalation_source: str | None = None

    # 1. Collect signals
    signals = await collect_all(collectors)

    # 2. Score urgency per depth
    scores = await compute_scores(db, signals, now=now)

    # 3. Classify depth
    bypass = source == "critical_bypass"
    decision = await classify_depth(db, scores, bypass_ceiling=bypass)

    classified_depth = decision.depth if decision else None
    trigger_reason = decision.reason if decision else reason

    # 3b. Check for pending light→deep escalation
    escalation_pending_id: str | None = None
    if cc_reflection_bridge is not None:
        try:
            # Fix 3A: expire stale escalations (>8h) before checking
            _STALE_ESCALATION_HOURS = 8
            all_pending = await observations.query(
                db, type="light_escalation_pending", resolved=False, limit=10,
            )
            for stale in all_pending:
                stale_created = stale.get("created_at", "")
                try:
                    stale_age = (
                        datetime.now(UTC) - datetime.fromisoformat(stale_created)
                    ).total_seconds() / 3600
                except (ValueError, TypeError):
                    stale_age = 999
                if stale_age >= _STALE_ESCALATION_HOURS:
                    await observations.resolve(
                        db, stale["id"],
                        resolved_at=now,
                        resolution_notes=f"Expired (age {stale_age:.1f}h > {_STALE_ESCALATION_HOURS}h TTL)",
                    )
                    logger.info("Auto-resolved stale escalation %s (%.1fh old)", stale["id"], stale_age)

            # Re-query after cleanup
            pending_escalations = await observations.query(
                db, type="light_escalation_pending", resolved=False, limit=1,
            )
            if pending_escalations:
                esc_created = pending_escalations[0].get("created_at", "")
                try:
                    esc_age_hours = (
                        datetime.now(UTC) - datetime.fromisoformat(esc_created)
                    ).total_seconds() / 3600
                except (ValueError, TypeError):
                    esc_age_hours = 999  # treat unparseable as expired

                if esc_age_hours < _STALE_ESCALATION_HOURS:
                    # Fix 2A: daily escalation budget (max 2 per 24h)
                    _ESCALATION_BUDGET_PER_DAY = 2
                    resolved_recent = await observations.query(
                        db, type="light_escalation_resolved", limit=20,
                    )
                    resolved_24h_count = 0
                    resolved_2h_count = 0
                    for r in resolved_recent:
                        r_created = r.get("created_at", "")
                        try:
                            r_age = (
                                datetime.now(UTC) - datetime.fromisoformat(r_created)
                            ).total_seconds() / 3600
                            if r_age < 2:
                                resolved_2h_count += 1
                            if r_age < 24:
                                resolved_24h_count += 1
                        except (ValueError, TypeError):
                            pass

                    # Check emergency bypass — critical signals override budget
                    esc_content = pending_escalations[0].get("content", "").lower()
                    is_emergency = any(kw in esc_content for kw in (
                        "critical_failure", "data_loss", "security_breach",
                        "all providers", "container memory critical",
                    ))

                    if resolved_2h_count >= 1 and not is_emergency:
                        logger.info("Light escalation cooldown active (2h), skipping")
                    elif resolved_24h_count >= _ESCALATION_BUDGET_PER_DAY and not is_emergency:
                        logger.info(
                            "Escalation budget exhausted (%d/%d in 24h), skipping",
                            resolved_24h_count, _ESCALATION_BUDGET_PER_DAY,
                        )
                    else:
                        if is_emergency:
                            logger.warning("Emergency escalation bypassing budget: %s", esc_content[:100])
                        classified_depth = Depth.DEEP
                        escalation_source = "light_escalation"
                        trigger_reason = f"light escalation: {pending_escalations[0].get('content', 'unknown')}"
                        logger.info("Forcing DEEP reflection due to light escalation")

                        # Fix 3B: defer resolution until after successful dispatch
                        escalation_pending_id = pending_escalations[0]["id"]
        except Exception:
            logger.warning("Failed to check light escalation state", exc_info=True)

    result = TickResult(
        tick_id=tick_id,
        timestamp=now,
        source=source,
        signals=signals,
        scores=scores,
        classified_depth=classified_depth,
        trigger_reason=trigger_reason,
        escalation_source=escalation_source,
        escalation_pending_id=escalation_pending_id,
        signal_staleness=get_staleness_context(),
    )

    # 4. Store tick result
    await awareness_ticks.create(
        db,
        id=tick_id,
        source=source,
        signals_json=json.dumps([
            {"name": s.name, "value": s.value, "source": s.source,
             "collected_at": s.collected_at}
            for s in signals
        ]),
        scores_json=json.dumps([
            {"depth": s.depth.value, "raw_score": s.raw_score,
             "time_multiplier": s.time_multiplier, "final_score": s.final_score,
             "threshold": s.threshold, "triggered": s.triggered}
            for s in scores
        ]),
        classified_depth=classified_depth.value if classified_depth else None,
        trigger_reason=trigger_reason,
        created_at=now,
    )

    # 5. If triggered, also create an observation (with content-hash dedup)
    if decision is not None:
        obs_content = json.dumps({
            "tick_id": tick_id,
            "depth": classified_depth.value,
            "reason": trigger_reason,
            "scores": {s.depth.value: s.final_score for s in scores},
        }, sort_keys=True)
        content_hash = hashlib.sha256(obs_content.encode()).hexdigest()
        is_dup = await observations.exists_by_hash(
            db, source="awareness_loop", content_hash=content_hash, unresolved_only=True,
        )
        if not is_dup:
            obs_id = str(uuid.uuid4())
            await observations.create(
                db,
                id=obs_id,
                source="awareness_loop",
                type="awareness_tick",
                content=obs_content,
                priority="high" if classified_depth in (Depth.DEEP, Depth.STRATEGIC) else "medium",
                created_at=now,
                content_hash=content_hash,
                skip_if_duplicate=True,
            )

    if not dispatch_reflection:
        return result

    if reflection_engine is not None and classified_depth == Depth.MICRO:
        ref_result = None
        try:
            ref_result = await reflection_engine.reflect(classified_depth, result, db=db)
        except Exception:
            logger.exception("Reflection crashed for tick %s", tick_id)

        if (ref_result is None or not ref_result.success) and deferred_queue:
            try:
                await deferred_queue.enqueue(
                    work_type="reflection",
                    call_site_id="reflection_micro",
                    priority=30,
                    payload=json.dumps({"tick_id": tick_id, "depth": "Micro"}),
                    reason="reflection_failed",
                    staleness_policy="ttl",
                    staleness_ttl_s=RATE_LIMIT_DEFERRAL_TTL_S,
                )
            except Exception:
                logger.warning("Failed to enqueue deferred reflection")

    if classified_depth == Depth.LIGHT and cc_reflection_bridge is None and reflection_engine is not None:
        try:
            await reflection_engine.reflect(classified_depth, result, db=db)
        except Exception:
            logger.exception("Light reflection fallback (API) failed for tick %s", tick_id)
            if deferred_queue:
                try:
                    await deferred_queue.enqueue(
                        work_type="reflection",
                        call_site_id="reflection_light",
                        priority=30,
                        payload=json.dumps({"tick_id": tick_id, "depth": "Light"}),
                        reason="reflection_failed",
                        staleness_policy="ttl",
                        staleness_ttl_s=RATE_LIMIT_DEFERRAL_TTL_S,
                    )
                except Exception:
                    logger.warning("Failed to enqueue deferred reflection")
    elif cc_reflection_bridge is not None and classified_depth in (Depth.LIGHT, Depth.DEEP, Depth.STRATEGIC):
        try:
            await cc_reflection_bridge.reflect(
                classified_depth,
                result,
                db=db,
                escalation_source=escalation_source if classified_depth == Depth.DEEP else None,
            )
            # Resolve escalation after successful dispatch
            if escalation_pending_id and classified_depth == Depth.DEEP:
                try:
                    await observations.resolve(
                        db, escalation_pending_id,
                        resolved_at=now,
                        resolution_notes="Escalation consumed by deep reflection",
                    )
                    await observations.create(
                        db,
                        id=str(uuid.uuid4()),
                        source="awareness_loop",
                        type="light_escalation_resolved",
                        content=f"Escalation {escalation_pending_id} consumed",
                        priority="low",
                        created_at=now,
                    )
                except Exception:
                    logger.warning("Failed to resolve escalation %s", escalation_pending_id, exc_info=True)
        except Exception:
            logger.exception("CC reflection failed for tick %s", tick_id)
            if deferred_queue and classified_depth:
                try:
                    await deferred_queue.enqueue(
                        work_type="reflection",
                        call_site_id=f"reflection_{classified_depth.value.lower()}",
                        priority=30,
                        payload=json.dumps({"tick_id": tick_id, "depth": classified_depth.value}),
                        reason="reflection_failed",
                        staleness_policy="ttl",
                        staleness_ttl_s=RATE_LIMIT_DEFERRAL_TTL_S,
                    )
                except Exception:
                    logger.warning("Failed to enqueue deferred reflection")

    return result


class AwarenessLoop:
    """The metronome — drives the 5-minute awareness tick via APScheduler."""

    def __init__(
        self,
        db: aiosqlite.Connection,
        collectors: list[SignalCollector],
        *,
        interval_minutes: int = 5,
        event_bus: GenesisEventBus | None = None,
        reflection_engine=None,
        cc_reflection_bridge=None,
        resilience_state_machine=None,
        deferred_queue=None,
    ):
        self._db = db
        self._collectors = list(collectors)
        self._interval = interval_minutes
        self._scheduler = AsyncIOScheduler()
        self._tick_lock = asyncio.Lock()
        self._event_bus = event_bus
        self._reflection_engine = reflection_engine
        self._cc_reflection_bridge = cc_reflection_bridge
        self._resilience_state_machine = resilience_state_machine
        self._deferred_queue = deferred_queue
        self._circuit_breakers: CircuitBreakerRegistry | None = None
        self._tick_event_loop: asyncio.AbstractEventLoop | None = None
        self._topic_manager = None
        self._guardian_watchdog = None
        self._remediation_registry = None
        self._sentinel = None
        self._credential_bridge_fn = None
        self._autonomous_cli_policy_export_fn = None
        self._briefing_writer_fn = None
        self._findings_ingest_fn = None
        self._session_observer_fn = None
        self._stopping: bool = False
        self._tick_count: int = 0
        self._last_tick_at: str | None = None
        self._last_tick_result: TickResult | None = None
        self._last_degradation_level: DegradationLevel | None = None

    def request_stop(self) -> None:
        """Signal that shutdown is imminent — skip deferred retries.

        Called from the bridge signal handler to prevent the ~650ms race
        between SIGTERM receipt and runtime.shutdown() reaching stop().
        Does NOT stop the scheduler — that happens in stop().
        """
        self._stopping = True

    @property
    def tick_count(self) -> int:
        """Total ticks since this loop instance started."""
        return self._tick_count

    @property
    def last_tick_at(self) -> str | None:
        """ISO timestamp of the most recent tick completion."""
        return self._last_tick_at

    def set_circuit_breakers(self, breakers: CircuitBreakerRegistry) -> None:
        """Inject circuit breaker registry for resilience state updates."""
        self._circuit_breakers = breakers

    async def _update_resilience_cognitive_state(self, level: DegradationLevel) -> None:
        """Write or clear cognitive state when resilience level changes."""
        try:
            from genesis.db.crud import cognitive_state

            now = datetime.now(UTC).isoformat()
            if level == DegradationLevel.NORMAL:
                content = "All providers normal — no degradation."
            else:
                # Identify which providers are down
                down = []
                if self._circuit_breakers:
                    down = [
                        name for name, cb in self._circuit_breakers._breakers.items()
                        if not cb.is_available()
                    ]
                detail = f"Providers down: {', '.join(sorted(down))}" if down else ""
                content = f"Resilience {level.value}: {detail}"

            await cognitive_state.replace_section(
                self._db,
                section="resilience_degradation",
                id=str(uuid.uuid4()),
                content=content,
                generated_by="awareness_loop",
                created_at=now,
            )
            logger.info("Resilience cognitive state updated: %s → %s", self._last_degradation_level, level)
        except Exception:
            logger.warning("Failed to update resilience cognitive state", exc_info=True)

    async def start(self) -> None:
        """Start the scheduler with the tick job.

        Uses next_run_time=now so the first tick fires immediately rather than
        waiting one full interval.  This keeps status.json fresh from the
        moment the bridge starts, preventing watchdog false-positives.
        """
        self._scheduler.add_job(
            self._on_tick,
            IntervalTrigger(minutes=self._interval),
            id="awareness_tick",
            max_instances=1,
            misfire_grace_time=60,
            next_run_time=datetime.now(UTC),
        )
        # Surface dropped-tick events. APScheduler emits these synchronously
        # on its own thread; bounce to our event loop via call_soon_threadsafe
        # so we can await event_bus.emit safely.
        try:
            self._tick_event_loop = asyncio.get_running_loop()
            self._scheduler.add_listener(
                self._on_scheduler_job_event,
                EVENT_JOB_MISSED | EVENT_JOB_MAX_INSTANCES,
            )
        except Exception:
            logger.warning(
                "Failed to register scheduler job-event listener", exc_info=True,
            )
        self._scheduler.start()
        logger.info("Awareness Loop started (interval=%dm, immediate first tick)", self._interval)

    def _on_scheduler_job_event(self, event) -> None:
        """APScheduler listener — runs in scheduler thread.

        Hand the event off to the asyncio loop so async emit can run safely.
        """
        if getattr(event, "job_id", None) != "awareness_tick":
            return
        event_code = getattr(event, "code", None)
        try:
            loop = self._tick_event_loop
            if loop is None or loop.is_closed():
                return
            loop.call_soon_threadsafe(
                lambda: asyncio.ensure_future(self._emit_tick_drop_event(event_code)),
            )
        except Exception:
            logger.warning("Failed to hand off scheduler event", exc_info=True)

    async def _emit_tick_drop_event(self, event_code: int | None) -> None:
        """Emit an observability event for a dropped / missed tick."""
        if self._event_bus is None:
            return
        if event_code == EVENT_JOB_MAX_INSTANCES:
            event_type = "tick.max_instances"
            message = (
                "Awareness tick dropped: previous tick still running "
                "(max_instances=1)"
            )
        elif event_code == EVENT_JOB_MISSED:
            event_type = "tick.missed"
            message = "Awareness tick missed (past misfire grace time)"
        else:
            event_type = "tick.dropped"
            message = f"Awareness tick dropped (code={event_code})"
        try:
            await self._event_bus.emit(
                Subsystem.AWARENESS,
                Severity.ERROR,
                event_type,
                message,
            )
        except Exception:
            logger.warning("Failed to emit tick drop event", exc_info=True)

    async def stop(self) -> None:
        """Stop the scheduler, waiting for any running tick to finish."""
        self._stopping = True
        self._scheduler.shutdown(wait=True)
        logger.info("Awareness Loop stopped")

    async def force_tick(self, reason: str) -> TickResult:
        """Critical event bypass — immediate out-of-cycle tick."""
        async with self._tick_lock:
            logger.info("Force tick triggered: %s", reason)
            result = await perform_tick(
                self._db, self._collectors,
                source="critical_bypass", reason=reason,
                reflection_engine=self._reflection_engine,
                cc_reflection_bridge=self._cc_reflection_bridge,
                deferred_queue=self._deferred_queue,
                dispatch_reflection=False,
            )

        if result.classified_depth is not None:
            from genesis.util.tasks import tracked_task

            tracked_task(
                self._dispatch_reflection(result),
                name=f"reflection-force-{result.tick_id[:8]}",
                subsystem=Subsystem.AWARENESS,
            )

        return result

    async def _on_tick(self) -> None:
        """Scheduled tick callback."""
        result = None
        async with self._tick_lock:
            try:
                result = await perform_tick(
                    self._db, self._collectors, source="scheduled",
                    reflection_engine=self._reflection_engine,
                    cc_reflection_bridge=self._cc_reflection_bridge,
                    deferred_queue=self._deferred_queue,
                    dispatch_reflection=False,
                )
                self._tick_count += 1
                self._last_tick_at = datetime.now(UTC).isoformat()
                self._last_tick_result = result
                if result.classified_depth:
                    logger.info(
                        "Tick triggered %s: %s",
                        result.classified_depth.value, result.trigger_reason,
                    )
                # Heartbeat — lets health MCP detect silent death
                if self._event_bus:
                    await self._event_bus.emit(
                        Subsystem.AWARENESS, Severity.DEBUG,
                        "heartbeat", "awareness_loop tick completed",
                    )
                try:
                    from genesis.runtime import GenesisRuntime
                    GenesisRuntime.instance().record_job_success("awareness_tick")
                except Exception:
                    pass  # Runtime may not be available in tests
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("Awareness tick failed")
                if self._event_bus:
                    await self._event_bus.emit(
                        Subsystem.AWARENESS, Severity.ERROR,
                        "tick.failed",
                        "Awareness tick failed with exception",
                    )
                try:
                    from genesis.runtime import GenesisRuntime
                    GenesisRuntime.instance().record_job_failure("awareness_tick", str(exc))
                except Exception:
                    pass

            # Update resilience cloud axis from circuit breaker state
            if self._resilience_state_machine and self._circuit_breakers:
                try:
                    level = self._circuit_breakers.compute_degradation_level()
                    cloud = _DEGRADATION_TO_CLOUD.get(level)
                    if cloud is None:
                        logger.warning("Unknown degradation level %s, defaulting to OFFLINE", level)
                        cloud = CloudStatus.OFFLINE
                    self._resilience_state_machine.update_cloud(cloud)

                    # Track degradation transitions in cognitive state
                    if level != self._last_degradation_level:
                        await self._update_resilience_cognitive_state(level)
                        self._last_degradation_level = level
                except Exception:
                    logger.warning("Resilience state update failed", exc_info=True)

            # Status file writes are handled by a dedicated loop in
            # runtime/init/memory.py (status-writer-loop). Decoupled from
            # the awareness tick so a slow tick (e.g. long Light reflection)
            # does not cause the watchdog to see a stale status.json.

            # Guardian bidirectional monitoring — check heartbeat, auto-recover
            if self._guardian_watchdog:
                try:
                    await self._guardian_watchdog.check_and_recover()
                except Exception:
                    logger.warning("Guardian watchdog check failed", exc_info=True)

            # Mechanical self-healing — run remediation registry against health probes
            if self._remediation_registry:
                try:
                    from genesis.observability.health import collect_probe_results
                    probe_results = await collect_probe_results(self._db)
                    outcomes = await self._remediation_registry.check_and_remediate(
                        probe_results,
                    )
                    acted = [o for o in outcomes if o.executed]
                    if acted:
                        logger.info(
                            "Remediation tick: %d actions executed (%s)",
                            len(acted),
                            ", ".join(o.action.name for o in acted),
                        )
                except Exception:
                    logger.warning("Remediation registry check failed", exc_info=True)

            # Propagate Telegram credentials to shared mount for Guardian
            if self._credential_bridge_fn:
                try:
                    self._credential_bridge_fn()
                except Exception:
                    logger.error("Credential bridge write failed", exc_info=True)

            if self._autonomous_cli_policy_export_fn:
                try:
                    self._autonomous_cli_policy_export_fn()
                except Exception:
                    logger.error("Autonomous CLI policy export failed", exc_info=True)

            # Write dynamic Guardian briefing to shared mount
            if self._briefing_writer_fn:
                try:
                    await self._briefing_writer_fn(self._db)
                except Exception:
                    logger.error("Guardian briefing write failed", exc_info=True)

            # Ingest Guardian diagnosis results from shared mount
            if self._findings_ingest_fn:
                try:
                    count = await self._findings_ingest_fn(self._db)
                    if count:
                        logger.info("Ingested %d Guardian findings", count)
                except Exception:
                    logger.error("Guardian findings ingest failed", exc_info=True)

            # Sentinel fire alarm check — evaluate conditions and dispatch if needed
            if self._sentinel:
                try:
                    await self._sentinel.check_fire_alarms()
                except Exception:
                    logger.warning("Sentinel fire alarm check failed", exc_info=True)

            # Session observer — process pending tool observations into memories
            if self._session_observer_fn:
                try:
                    obs_result = await self._session_observer_fn()
                    if obs_result and obs_result.notes_stored > 0:
                        logger.info(
                            "Session observer: %d notes from %d observations",
                            obs_result.notes_stored, obs_result.observations_read,
                        )
                except Exception:
                    logger.warning("Session observer processing failed", exc_info=True)

        if result is None:
            return

        # Kill switch — tick/heartbeats still run but no dispatches when paused
        try:
            from genesis.runtime import GenesisRuntime
            if GenesisRuntime.instance().paused:
                logger.debug("Skipping reflection dispatch (Genesis paused)")
                return
        except Exception:
            pass

        from genesis.util.tasks import tracked_task

        if result.classified_depth is not None:
            tracked_task(
                self._dispatch_reflection(result),
                name=f"reflection-{result.classified_depth.value.lower()}-{result.tick_id[:8]}",
                subsystem=Subsystem.AWARENESS,
            )

        if not self._stopping:
            tracked_task(
                self._retry_deferred_if_pending(result),
                name=f"deferred-retry-{result.tick_id[:8]}",
                subsystem=Subsystem.AWARENESS,
            )
            tracked_task(
                self._resume_approved_reflections(),
                name=f"approval-resume-{result.tick_id[:8]}",
                subsystem=Subsystem.AWARENESS,
            )
            tracked_task(
                self._resume_approved_sentinel_dispatches(),
                name=f"sentinel-resume-{result.tick_id[:8]}",
                subsystem=Subsystem.AWARENESS,
            )

    async def _dispatch_reflection(self, result: TickResult) -> None:
        depth = result.classified_depth
        if depth is None:
            return

        tick_id = result.tick_id
        db = self._db
        logger.info(
            "Dispatch reflection: depth=%s, tick=%s, bridge=%s, engine=%s",
            depth.value, tick_id[:8],
            self._cc_reflection_bridge is not None,
            self._reflection_engine is not None,
        )

        if self._reflection_engine is not None and depth == Depth.MICRO:
            ref_result = None
            try:
                ref_result = await self._reflection_engine.reflect(depth, result, db=db)
            except Exception:
                logger.exception("Reflection crashed for tick %s", tick_id)

            if ref_result and ref_result.success and self._event_bus:
                try:
                    await self._event_bus.emit(
                        Subsystem.REFLECTION, Severity.DEBUG,
                        "heartbeat", "micro-reflection completed",
                    )
                except Exception:
                    logger.warning("Failed to emit reflection heartbeat", exc_info=True)

            # Post micro reflection to supergroup topic
            if ref_result and ref_result.success and ref_result.output and self._topic_manager:
                micro = ref_result.output
                try:
                    anomaly_flag = " [ANOMALY]" if micro.anomaly else ""
                    tags_str = ", ".join(micro.tags[:5]) if micro.tags else ""
                    text = (
                        f"<b>Micro Reflection</b>{anomaly_flag}\n\n"
                        f"{micro.summary}\n\n"
                        f"<i>Salience: {micro.salience:.2f}"
                        f"{f' | Tags: {tags_str}' if tags_str else ''}</i>"
                    )
                    await self._topic_manager.send_to_category("reflection_micro", text)
                except Exception:
                    logger.warning("Failed to post micro reflection to topic", exc_info=True)

            if (ref_result is None or not ref_result.success) and self._deferred_queue:
                try:
                    await self._deferred_queue.enqueue(
                        work_type="reflection",
                        call_site_id="reflection_micro",
                        priority=30,
                        payload=json.dumps({"tick_id": tick_id, "depth": "Micro"}),
                        reason="reflection_failed",
                        staleness_policy="ttl",
                        staleness_ttl_s=RATE_LIMIT_DEFERRAL_TTL_S,
                    )
                except Exception:
                    logger.warning("Failed to enqueue deferred reflection")
            return

        if depth == Depth.LIGHT and self._cc_reflection_bridge is None and self._reflection_engine is not None:
            try:
                await self._reflection_engine.reflect(depth, result, db=db)
            except Exception:
                logger.exception("Light reflection fallback (API) failed for tick %s", tick_id)
                if self._deferred_queue:
                    try:
                        await self._deferred_queue.enqueue(
                            work_type="reflection",
                            call_site_id="reflection_light",
                            priority=30,
                            payload=json.dumps({"tick_id": tick_id, "depth": "Light"}),
                            reason="reflection_failed",
                            staleness_policy="ttl",
                            staleness_ttl_s=RATE_LIMIT_DEFERRAL_TTL_S,
                        )
                    except Exception:
                        logger.warning("Failed to enqueue deferred reflection")
            return

        if self._cc_reflection_bridge is not None and depth in (Depth.LIGHT, Depth.DEEP, Depth.STRATEGIC):
            try:
                await self._cc_reflection_bridge.reflect(
                    depth,
                    result,
                    db=db,
                    escalation_source=result.escalation_source if depth == Depth.DEEP else None,
                )
                # Fix 3B: resolve escalation AFTER successful dispatch
                if result.escalation_pending_id and depth == Depth.DEEP:
                    await self._resolve_escalation(result.escalation_pending_id, result.timestamp)
            except Exception:
                logger.exception("CC reflection failed for tick %s", tick_id)
                if result.escalation_pending_id:
                    logger.info(
                        "Escalation %s left pending (dispatch failed, will retry)",
                        result.escalation_pending_id,
                    )
                if self._deferred_queue:
                    try:
                        await self._deferred_queue.enqueue(
                            work_type="reflection",
                            call_site_id=f"reflection_{depth.value.lower()}",
                            priority=30,
                            payload=json.dumps({"tick_id": tick_id, "depth": depth.value}),
                            reason="reflection_failed",
                            staleness_policy="ttl",
                            staleness_ttl_s=RATE_LIMIT_DEFERRAL_TTL_S,
                        )
                    except Exception:
                        logger.warning("Failed to enqueue deferred reflection")

    async def _retry_deferred_if_pending(self, current_tick: TickResult) -> None:
        try:
            await self._retry_deferred_reflection(current_tick)
        except Exception:
            logger.warning("Deferred reflection retry failed", exc_info=True)

    async def _resume_approved_reflections(self) -> None:
        """Resume deep/strategic reflections whose approvals were granted.

        When a user approves a deep reflection via Telegram or dashboard,
        the awareness loop's scoring may never independently reach the "Deep"
        threshold again. This method checks for approved-but-unconsumed
        reflection approvals and dispatches them immediately.
        """
        if not self._cc_reflection_bridge:
            return
        # The autonomous dispatcher is set on the reflection bridge, not
        # directly on the awareness loop. Access the gate via the bridge.
        dispatcher = getattr(self._cc_reflection_bridge, "_autonomous_dispatcher", None)
        if dispatcher is None:
            return
        gate = getattr(dispatcher, "approval_gate", None)
        if gate is None:
            return

        tick = self._last_tick_result
        if tick is None:
            return  # No tick yet — can't build reflection prompt

        for depth_name in ("deep", "strategic"):
            try:
                approved = await gate.find_recently_approved(
                    subsystem="reflection",
                    policy_id=f"reflection_{depth_name}",
                )
                if not approved:
                    continue
                # Atomic consume — prevents double-dispatch across ticks
                consumed = await gate.mark_consumed(approved["id"])
                if not consumed:
                    continue  # Another tick already consumed it
                depth = Depth.DEEP if depth_name == "deep" else Depth.STRATEGIC
                logger.info(
                    "Resuming %s reflection from approved request %s",
                    depth_name, approved["id"][:8],
                )
                await self._cc_reflection_bridge.reflect(
                    depth, tick, db=self._db,
                )
            except Exception:
                logger.error(
                    "Failed to resume %s reflection", depth_name, exc_info=True,
                )

    async def _resume_approved_sentinel_dispatches(self) -> None:
        """Resume sentinel dispatches whose approvals were granted.

        Mirrors _resume_approved_reflections: checks for approved-but-
        unconsumed sentinel approvals and resumes the dispatcher.
        """
        if self._sentinel is None:
            return

        # Access the approval gate via the sentinel dispatcher
        gate = getattr(self._sentinel, "_approval_gate", None)
        if gate is None:
            return

        from genesis.sentinel.state import SentinelState as _SS

        # Only resume if sentinel is actually waiting for an approval
        state = self._sentinel.state
        if state.state not in (_SS.AWAITING_DISPATCH_APPROVAL, _SS.AWAITING_ACTION_APPROVAL):
            return

        for policy_id in ("sentinel_dispatch", "sentinel_action"):
            try:
                approved = await gate.find_recently_approved(
                    subsystem="sentinel",
                    policy_id=policy_id,
                )
                if not approved:
                    continue

                # Atomic consume — prevents double-dispatch
                consumed = await gate.mark_consumed(approved["id"])
                if not consumed:
                    continue

                logger.info(
                    "Resuming sentinel %s from approved request %s",
                    policy_id, approved["id"][:8],
                )
                await self._sentinel.resume_from_approval(
                    approved["id"], "approved",
                )
            except Exception:
                logger.error(
                    "Failed to resume sentinel %s", policy_id, exc_info=True,
                )

    async def _retry_deferred_reflection(self, current_tick: TickResult) -> None:
        """Retry ONE deferred reflection per tick using current tick's fresh data.

        Rate-limited: one item per 5-min tick. On failure, attempts increment
        and the item stays pending. After 3 failed attempts, escalate via
        WARNING event (not silently discarded).
        """
        if not self._deferred_queue:
            return

        if self._stopping:
            logger.debug("Skipping deferred reflection retry — loop is stopping")
            return

        item = await self._deferred_queue.next_pending(max_priority=40)
        if not item or item.get("work_type") != "reflection":
            return

        item_id = item["id"]
        payload = json.loads(item.get("payload_json", "{}"))
        depth_str = payload.get("depth", "")

        try:
            depth = Depth(depth_str)
        except ValueError:
            logger.warning("Deferred reflection has invalid depth=%s, discarding", depth_str)
            await self._deferred_queue.mark_discarded(item_id, f"invalid depth: {depth_str}")
            return

        attempts = item.get("attempts", 0)
        await self._deferred_queue.mark_processing(item_id)
        logger.info(
            "Retrying deferred reflection: id=%s depth=%s attempt=%d",
            item_id, depth.value, attempts + 1,
        )

        try:
            if depth in (Depth.LIGHT, Depth.DEEP, Depth.STRATEGIC) and self._cc_reflection_bridge:
                result = await self._cc_reflection_bridge.reflect(depth, current_tick, db=self._db)
            elif self._reflection_engine:
                result = await self._reflection_engine.reflect(depth, current_tick, db=self._db)
            else:
                logger.warning(
                    "No reflection handler for depth=%s — leaving pending", depth.value,
                )
                await self._deferred_queue.reset_to_pending(item_id)
                return

            if result.success:
                await self._deferred_queue.mark_completed(item_id)
                logger.info("Deferred reflection succeeded: id=%s depth=%s", item_id, depth.value)
            else:
                # Operational failure (rate limit, throttle, etc.) — reset for retry.
                # Don't count as a discard-worthy attempt; TTL handles expiry.
                await self._deferred_queue.reset_to_pending(item_id)
                logger.info(
                    "Deferred reflection not ready: id=%s depth=%s reason=%s — will retry",
                    item_id, depth.value, result.reason or "unknown",
                )
        except Exception:
            new_attempts = attempts + 1  # mark_processing already incremented in DB
            logger.warning(
                "Deferred reflection retry failed: id=%s depth=%s attempt=%d",
                item_id, depth.value, new_attempts, exc_info=True,
            )
            if new_attempts >= 3:
                await self._deferred_queue.mark_discarded(
                    item_id,
                    f"max attempts ({new_attempts}) exceeded — retry failed",
                )
                if self._event_bus:
                    await self._event_bus.emit(
                        Subsystem.AWARENESS, Severity.WARNING,
                        "deferred.max_attempts",
                        f"Deferred {depth.value} reflection failed after {new_attempts} attempts",
                    )
            else:
                # Reset to pending so next tick can retry
                await self._deferred_queue.reset_to_pending(item_id)

    async def _resolve_escalation(self, pending_id: str, now: str) -> None:
        """Resolve a consumed escalation and record the cooldown marker."""
        from genesis.db.crud import observations

        try:
            await observations.resolve(
                self._db, pending_id,
                resolved_at=now,
                resolution_notes="Escalation consumed by deep reflection",
            )
            await observations.create(
                self._db,
                id=str(uuid.uuid4()),
                source="awareness_loop",
                type="light_escalation_resolved",
                content=f"Escalation {pending_id} consumed",
                priority="low",
                created_at=now,
            )
            logger.info("Escalation %s resolved after successful dispatch", pending_id)
        except Exception:
            logger.warning("Failed to resolve escalation %s", pending_id, exc_info=True)

    def set_resilience_state_machine(self, sm) -> None:
        """Inject resilience state machine after construction."""
        self._resilience_state_machine = sm

    def set_deferred_queue(self, dq) -> None:
        """Inject deferred queue after construction."""
        self._deferred_queue = dq

    def set_reflection_engine(self, engine) -> None:
        """Inject reflection engine after construction."""
        self._reflection_engine = engine

    def set_cc_reflection_bridge(self, bridge) -> None:
        """Inject CC reflection bridge after construction."""
        self._cc_reflection_bridge = bridge

    def set_topic_manager(self, manager) -> None:
        """Inject TopicManager for posting micro reflections to forum topics."""
        self._topic_manager = manager

    def set_guardian_watchdog(self, watchdog) -> None:
        """Inject Guardian watchdog for bidirectional host monitoring."""
        self._guardian_watchdog = watchdog

    def set_remediation_registry(self, registry) -> None:
        """Inject remediation registry for mechanical self-healing."""
        self._remediation_registry = registry

    def set_sentinel(self, sentinel) -> None:
        """Inject Sentinel dispatcher for autonomous fire alarm response."""
        self._sentinel = sentinel

    def set_credential_bridge(self, fn) -> None:
        """Inject credential bridge for Telegram credential propagation."""
        self._credential_bridge_fn = fn

    def set_autonomous_cli_policy_exporter(self, fn) -> None:
        """Inject shared-mount exporter for effective autonomous CLI policy."""
        self._autonomous_cli_policy_export_fn = fn

    def set_briefing_writer(self, fn) -> None:
        """Inject dynamic briefing writer for Guardian context updates."""
        self._briefing_writer_fn = fn

    def set_findings_ingest(self, fn) -> None:
        """Inject Guardian findings ingest for reading diagnosis results."""
        self._findings_ingest_fn = fn

    def set_session_observer(self, fn) -> None:
        """Inject session observer processor for tool activity notes."""
        self._session_observer_fn = fn

    def replace_collectors(self, collectors: list) -> None:
        """Replace signal collectors (late-binding upgrade from stubs to real).

        WARNING: this is a **full replacement**, not a superset merge. Any
        collector registered by ``runtime/init/awareness.py`` that should
        survive the swap MUST be re-added to the new ``collectors`` list
        passed by ``runtime/init/learning.py``. Otherwise it is silently
        dropped from the awareness loop and its signal stops being measured.

        Currently both ``ContainerMemoryCollector`` and ``JobHealthCollector``
        are registered in awareness init but NOT re-listed in the learning
        swap, so their signals (`container_memory_pct`, `scheduled_job_health`)
        are dropped post-bootstrap. This is functionally OK today because
        neither has a corresponding ``signal_weights`` row, but adding such
        a row in the future will silently produce 0.0 readings unless the
        learning swap is updated to re-include them.
        """
        self._collectors = list(collectors)

    # GROUNDWORK(category-2-rhythms): add_rhythm(name, interval, callback)
    # GROUNDWORK(category-3-crons): add_cron(name, cron_expr, callback)
