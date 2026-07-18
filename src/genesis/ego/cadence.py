"""Ego cadence manager — controls when the ego runs.

Owns an APScheduler with jobs:
1. IntervalTrigger for regular proactive cycles (adaptive backoff)
2. CronTrigger for the mandatory morning report
3. IntervalTrigger for the 30-min mechanical sweep (proposal expiry/dispatch)
4. CronTrigger for goal staleness scanning (user ego only, twice daily)

All cycle types (proactive, morning report, reactive, escalation) flow
through the unified signal consumer loop: signal sources push EgoSignal
objects to the SignalQueue, and _signal_consumer_loop() drains + runs
run_unified_cycle().
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from genesis.ego.session import CycleBlockedError
from genesis.ego.signals import EgoSignal, SignalQueue
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


def _expires_in(minutes: float) -> str:
    """ISO timestamp *minutes* from now — signal TTL helper.

    TTLs keep requeued/parked signals from going stale in the queue:
    each producer sets a lifetime matching how long its signal stays
    actionable (a proactive tick is superseded by the next one; a
    morning briefing is stale by afternoon). Escalations get NO TTL —
    critical facts don't expire on their own.
    """
    return (datetime.now(UTC) + timedelta(minutes=minutes)).isoformat()


def _now_utc() -> datetime:
    """UTC now — indirection so tests can control time deterministically."""
    return datetime.now(UTC)


def _local_now(tz: object) -> datetime:
    """Now in *tz* — indirection so quiet-hours tests can pin the clock.

    ``tz`` is typically the IANA string from :func:`user_timezone`; convert to
    a tzinfo (falling back to UTC on an unknown name) since ``datetime.now``
    rejects a bare string.
    """
    if isinstance(tz, str):
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            tz = ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError):
            tz = UTC
    return datetime.now(tz)  # type: ignore[arg-type]


def _map_priority(severity_or_priority: str) -> str:
    """Map event severity/priority strings to signal priority levels."""
    s = severity_or_priority.lower()
    if s in ("critical",):
        return "critical"
    if s in ("error", "high"):
        return "high"
    if s in ("warning", "medium"):
        return "medium"
    return "low"


class EgoCadenceManager:
    """Manages when the ego runs.

    Responsibilities:
    - APScheduler job registration (interval + morning cron)
    - Activity detection gate (IdleDetector)
    - Adaptive backoff (double interval on idle cycles, reset on proposals)
    - Circuit breaker (N consecutive failures -> pause)
    - Pause/resume controls

    All cycle dispatch goes through ``session.run_unified_cycle()`` via
    the signal consumer loop.
    """

    def __init__(
        self,
        *,
        session: EgoSession,
        config: EgoConfig,
        idle_detector: IdleDetector | None = None,
        db: aiosqlite.Connection,
        event_bus: GenesisEventBus | None = None,
        autonomy_manager: object | None = None,
    ) -> None:
        self._session = session
        self._config = config
        self._idle_detector = idle_detector
        self._db = db
        self._event_bus = event_bus
        # AutonomyManager (duck-typed: detect_earnback_candidates / promote),
        # used by the user-ego earn-back check. None disables earn-back.
        self._autonomy_manager = autonomy_manager

        self._scheduler = AsyncIOScheduler()
        self._paused = False
        self._running = False

        # Circuit breaker state
        self._consecutive_failures = 0
        self._circuit_open_until: datetime | None = None

        # Adaptive interval
        self._current_interval = config.cadence_minutes

        # Last time a proactive tick actually pushed a signal — the quiet-hours
        # floor throttles overnight ticks relative to this.
        self._last_proactive_fire_at: datetime | None = None

        # Prevent concurrent cycles (interval + morning could overlap)
        self._lock = asyncio.Lock()

        # Deep-think counter: every Nth proactive cycle uses Opus instead
        # of the ego's base model. Only meaningful for egos that run Sonnet
        # by default (Genesis ego). User ego already runs Opus proactive.
        self._deep_think_interval = 5
        self._proactive_cycle_count = 0

        # Unified signal queue: all signal sources push here, consumer loop drains.
        # Proactive (_on_tick), morning report (_on_morning_report), reactive
        # (push_reactive_event), and escalation (push_escalation_event) all
        # converge on this queue.
        self._signal_queue = SignalQueue()
        self._signal_consumer_task: asyncio.Task | None = None

        # Reactive rate limiting: max N reactive-focused cycles per hour.
        # Checked at the consumer (not at push time) to preserve batch semantics —
        # a burst of events batches into one cycle consuming one rate-limit slot.
        self._reactive_max_per_hour = 3
        self._reactive_timestamps: list[datetime] = []

    # -- Lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Register APScheduler jobs and start."""
        # NOTE: this job intentionally keeps IntervalTrigger despite the
        # ">1h intervals should use CronTrigger" convention (CLAUDE.md Traps) —
        # the ego's interval is variable/adaptive (base → up to 72h) and a
        # wall-clock CronTrigger can't express a changing interval. The
        # restart-safe boot first-fire below is the mitigation for the reset trap.
        #
        # Boot first-fire: anchor to this ego's own persisted
        # job_health.last_success so a restart cannot starve the proactive
        # cycle by a full (backed-off) interval — the documented
        # IntervalTrigger-resets-on-restart trap. None => fresh install =>
        # OMIT next_run_time (never pass None — APScheduler treats an explicit
        # next_run_time=None as a PAUSED job).
        # Restore the persisted adaptive interval so a restart resumes the
        # backed-off cadence instead of resetting to base (the in-memory-only
        # reset that pinned the ego at base cadence under restart churn).
        await self._restore_interval()
        boot_first_fire = await self._compute_boot_first_fire()
        ego_cycle_kwargs: dict[str, object] = {
            "id": "ego_cycle",
            "max_instances": 1,
            "misfire_grace_time": 300,
        }
        if boot_first_fire is not None:
            ego_cycle_kwargs["next_run_time"] = boot_first_fire
        self._scheduler.add_job(
            self._on_tick,
            IntervalTrigger(minutes=self._current_interval),
            **ego_cycle_kwargs,
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
        # Liveness pulse: emit a heartbeat every 5 min on a fixed, never-
        # rescheduled cadence so health monitoring tracks "ego subsystem alive"
        # independent of the proactive _on_tick interval. _on_tick's interval
        # stretches via adaptive backoff (up to 72h) and is deferred by
        # reschedule_job on every completed cycle, so a heartbeat tied to it
        # routinely exceeds the 4h overdue threshold during normal operation
        # even while the ego is healthy and cycling. Mirrors the fixed-interval
        # liveness ticks of awareness/surplus/dashboard.
        self._scheduler.add_job(
            self._on_heartbeat,
            IntervalTrigger(minutes=5),
            id="ego_heartbeat",
            max_instances=1,
            misfire_grace_time=60,
        )
        # Goal staleness scanner: push goal_review signals for stale goals.
        # User ego only — USER goals are user-ego jurisdiction. (The genesis
        # ego reviews its own origin='genesis_ego' goals through a separate
        # path: an own-goals context section + parsed output keys, never this
        # scanner.) Skipped at runtime via source_tag check but also gate
        # registration for clarity.
        if self._session._source_tag == "user_ego_cycle":
            self._scheduler.add_job(
                self._check_stale_goals,
                CronTrigger(hour="10,16", timezone=user_timezone()),
                id="ego_goal_staleness",
                max_instances=1,
                misfire_grace_time=600,
            )
        self._scheduler.start()
        from genesis.util.tasks import tracked_task

        self._signal_consumer_task = tracked_task(
            self._signal_consumer_loop(),
            name=f"ego_signal_consumer_{id(self)}",
            event_bus=self._event_bus,
            logger=logger,
        )
        self._running = True
        # Initial liveness pulse so a restart doesn't look stale to
        # subsystem_heartbeats during the first 5-min ego_heartbeat window
        # (mirrors awareness/surplus emitting a heartbeat at start).
        self._emit_heartbeat("start")
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
        """Shut down APScheduler and signal consumer.

        Awaits task cancellation so any held lock is released before
        stop() returns — prevents deadlock if caller re-acquires the lock.
        """
        task = self._signal_consumer_task
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            self._signal_consumer_task = None
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
    def source_tag(self) -> str:
        """This ego's source tag ('user_ego_cycle' / 'genesis_ego_cycle')."""
        return self._session._source_tag

    @property
    def next_fire_at(self) -> str | None:
        """ISO timestamp of the next scheduled proactive cycle, or None.

        Reflects APScheduler's live next_run_time for the ``ego_cycle`` job,
        so the dashboard shows the real next fire (which adaptive backoff and
        per-cycle reschedules move), not a computed guess.
        """
        try:
            job = self._scheduler.get_job("ego_cycle")
            if job is not None and job.next_run_time is not None:
                return job.next_run_time.isoformat()
        except Exception:
            logger.debug("next_fire_at lookup failed", exc_info=True)
        return None

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    # -- Reactive / escalation signal emitters --------------------------------

    def push_reactive_event(self, event: dict) -> None:
        """Push an event that may trigger a reactive ego cycle.

        Creates an EgoSignal and pushes to the unified signal queue.
        Content dedup is handled by SignalQueue (per-category window on
        ``reactive:{summary}``). Rate limiting happens at the consumer
        (``_process_signals``) to preserve batch semantics.

        event keys: {"type": str, "summary": str, "priority": str?, "source": str?}
        """
        if not self._running or self._paused:
            return

        # No model/effort override: each ego's base config rules. Forcing
        # Opus/high here caused provider-exhaustion storms on event bursts.
        signal = EgoSignal(
            signal_type="event",
            focus_category="reactive",
            summary=event.get("summary", "")[:200],
            priority=_map_priority(event.get("priority", "high")),
            metadata={
                "event_type": event.get("type"),
                "source": event.get("source"),
            },
            # 12h: the deadline scanner re-fires and events re-emit; a
            # half-day-old reactive signal is stale context, not news.
            expires_at=_expires_in(12 * 60),
        )
        if self._signal_queue.push(signal):
            logger.debug("Reactive signal pushed: %s", event.get("type", "?"))

    def push_escalation_event(self, event: dict) -> None:
        """Push a critical event as an escalation signal.

        Escalation signals get ``focus_category="escalation"`` which
        selects escalation-specific context weights and prompt directive.
        No rate limit — escalation signals are critical and rare.

        event keys: {"type": str, "summary": str, "priority": str?, "source": str?}
        """
        if not self._running or self._paused:
            return

        # No model override: each ego's base config rules. Effort is forced
        # to high — never think less about a CRITICAL escalation than a
        # routine tick.
        signal = EgoSignal(
            signal_type="event",
            focus_category="escalation",
            summary=event.get("summary", "")[:200],
            priority="critical",
            metadata={
                "effort_override": "high",
                "event_type": event.get("type"),
                "source": event.get("source"),
            },
            # No expires_at: critical facts don't expire on their own.
        )
        if self._signal_queue.push(signal):
            logger.info("Escalation signal pushed: %s", event.get("type", "?"))

    # -- Sweep helpers -----------------------------------------------------

    async def _sweep_with_expiry(self) -> None:
        """Expire stale proposals, auto-table old ones, dispatch approved, check deadlines."""
        try:
            from genesis.db.crud import ego as ego_crud

            expired = await ego_crud.expire_stale_proposals(self._session._db)
            if expired:
                logger.info("Pre-sweep expiry: %d proposal(s) expired", expired)

            auto_tabled = await ego_crud.auto_table_stale_proposals(
                self._session._db,
            )
            if auto_tabled:
                logger.info(
                    "Pre-sweep auto-table: %d proposal(s) tabled (>14d)",
                    auto_tabled,
                )
        except Exception:
            logger.warning("Pre-sweep expiry/auto-table failed", exc_info=True)

        await self._session.sweep_approved_proposals()

        # Deadline scanner: push reactive events for approaching deadlines
        await self._check_approaching_deadlines()

        # Autonomy earn-back: propose restoring earned levels whose evidence
        # now supports it (user ego only; no-op otherwise).
        await self._check_earnback_opportunities()

        # Cell promotion (WS-8 PR-D): propose standing autonomy for email
        # capability cells whose approved competence now supports it (user ego
        # only; the user approves each promotion).
        await self._check_cell_promotion_opportunities()

    async def _check_approaching_deadlines(self) -> None:
        """Push reactive events for events approaching within 48h."""
        try:
            from genesis.db.crud import memory_events

            events = await memory_events.approaching_deadlines(
                self._session._db,
                days=2,
                limit=5,
            )
            if not events:
                return

            for evt in events:
                subj = evt.get("subject", "?")
                verb = evt.get("verb", "?")
                obj = evt.get("object", "")
                date = evt.get("event_date", "")[:10]
                self.push_reactive_event(
                    {
                        "type": "deadline_approaching",
                        "summary": f"{subj} {verb} {obj} on {date}",
                        "priority": "high",
                        "source": "deadline_scanner",
                    }
                )
            logger.debug("Deadline scanner found %d approaching event(s)", len(events))
        except Exception:
            logger.debug("Deadline scanner failed", exc_info=True)

    # -- Goal staleness scanner ------------------------------------------------

    async def _check_stale_goals(self) -> None:
        """Push goal_review signals for goals stale beyond threshold.

        Queries user_goals.updated_at and pushes one signal per stale goal.
        User ego only, USER goals only (origin='user') — ego-owned goals are
        reviewed by the genesis ego's own path, and a user-ego review of an
        ego goal would surface approval proposals for internal ops goals.
        Registration is gated in start() but we double-check source_tag here
        as defense-in-depth.
        """
        if self._session._source_tag != "user_ego_cycle":
            return
        if not self._should_run(skip_idle_check=True):
            return

        try:
            from genesis.db.crud import ego as ego_crud
            from genesis.db.crud import user_goals
            from genesis.ego.types import GOAL_STUCK_EXECUTED_THRESHOLD

            goals = await user_goals.list_active(
                self._session._db,
                origin="user",
            )
            if not goals:
                return

            global_threshold = self._config.goal_review_staleness_days
            now = datetime.now(UTC)
            pushed = 0

            for g in goals:
                updated_at = g.get("updated_at") or g.get("created_at") or ""
                if not updated_at:
                    continue
                try:
                    updated = datetime.fromisoformat(updated_at)
                    if updated.tzinfo is None:
                        updated = updated.replace(tzinfo=UTC)
                    days_stale = (now - updated).days
                except (ValueError, TypeError):
                    continue

                # Per-goal cadence override (cadence_days > 0), else global
                per_goal = g.get("cadence_days")
                threshold_days = (
                    per_goal if isinstance(per_goal, int) and per_goal > 0 else global_threshold
                )
                if days_stale < threshold_days:
                    continue

                # Distinguish "stuck" (effort spent, no progress) from "stale"
                # (untouched): a still-active goal with >= N executed proposals
                # has been worked on without advancing. Stuck goals get a
                # higher-priority signal so the ego replans rather than nudges.
                # Best-effort — a query failure yields {} → treated as stale.
                summary_counts = await ego_crud.get_goal_proposal_summary(
                    self._session._db,
                    g["id"],
                )
                executed = summary_counts.get("executed", 0)
                is_stuck = executed >= GOAL_STUCK_EXECUTED_THRESHOLD

                title = (g.get("title") or "?")[:80]
                if is_stuck:
                    sig_summary = (
                        f"Goal stuck ({days_stale}d, {executed} executed, not advancing): {title}"
                    )
                    sig_priority = "high"
                else:
                    sig_summary = f"Goal stale ({days_stale}d): {title}"
                    sig_priority = "medium"

                signal = EgoSignal(
                    signal_type="timer",
                    focus_category="goal_review",
                    summary=sig_summary,
                    priority=sig_priority,
                    focus_id=g["id"],
                    metadata={
                        "mode": "stuck" if is_stuck else "stale",
                        "executed_proposals": executed,
                    },
                    # 24h: the next staleness scan re-detects anything
                    # still stale.
                    expires_at=_expires_in(24 * 60),
                )
                if self._signal_queue.push(signal):
                    pushed += 1

            if pushed:
                logger.info(
                    "Goal staleness scanner: %d signal(s) pushed",
                    pushed,
                )
        except Exception:
            logger.debug("Goal staleness scanner failed", exc_info=True)

    # -- Autonomy earn-back ------------------------------------------------

    _EARNBACK_REJECT_COOLDOWN_DAYS = 7

    async def _check_earnback_opportunities(self) -> None:
        """Propose restoring earned autonomy for categories whose evidence now
        supports it. User-ego only; evidence-gated; the user approves each
        promotion. Never auto-promotes.
        """
        if self._session._source_tag != "user_ego_cycle":
            return
        if self._autonomy_manager is None:
            return
        try:
            candidates = await self._autonomy_manager.detect_earnback_candidates()
            if not candidates:
                return

            from genesis.db.crud import ego as ego_crud

            # Anti-spam: skip categories that already have a pending earn-back
            # proposal (also covered by create_batch content-hash dedup) or an
            # active reject cooldown.
            pending = await ego_crud.list_pending_proposals(self._session._db)
            pending_cats = {
                p.get("action_category")
                for p in pending
                if p.get("action_type") == "autonomy_earnback"
            }

            to_make: list[dict] = []
            for cand in candidates:
                category = cand["category"]
                if category in pending_cats:
                    continue
                if await self._earnback_in_cooldown(category):
                    continue
                to_make.append(self._build_earnback_proposal(cand))

            if not to_make:
                return

            batch_id, ids, _ = await self._session._proposals.create_batch(
                to_make,
                ego_source="user_ego_cycle",
            )
            if ids:
                await self._session._proposals.send_digest(
                    batch_id,
                    ego_source="user_ego_cycle",
                )
                logger.info(
                    "Earn-back: proposed promotion for %d categor(ies)",
                    len(ids),
                )
        except Exception:
            logger.warning("Earn-back opportunity check failed", exc_info=True)

    async def _earnback_in_cooldown(self, category: str) -> bool:
        """True if *category*'s earn-back was rejected within the cooldown window."""
        from genesis.db.crud import ego as ego_crud

        ts = await ego_crud.get_state(
            self._session._db,
            f"earnback_reject:{category}",
        )
        if not ts:
            return False
        try:
            rejected_at = datetime.fromisoformat(ts)
        except (ValueError, TypeError):
            return False
        if rejected_at.tzinfo is None:
            rejected_at = rejected_at.replace(tzinfo=UTC)
        age_days = (datetime.now(UTC) - rejected_at).days
        return age_days < self._EARNBACK_REJECT_COOLDOWN_DAYS

    @staticmethod
    def _build_earnback_proposal(cand: dict) -> dict:
        """Build a proposal dict for an earn-back candidate."""
        category = cand["category"]
        current = cand["current_level"]
        target = cand["target_level"]
        posterior = cand.get("posterior")
        conf = round(float(posterior), 2) if posterior is not None else 0.7
        regressed_on = (cand.get("last_regression_at") or "")[:10]
        since = f" (demoted {regressed_on})" if regressed_on else ""
        content = (
            f"Restore {category} autonomy to L{target}. It was reduced to "
            f"L{current}{since} after a Bayesian regression; the success record "
            f"has since recovered enough to support L{target} again "
            f"(posterior {conf})."
        )
        rationale = (
            "Evidence-gated earn-back: the category's own success/correction "
            "history now supports the earned level. Approve to restore it; "
            "reject to keep it where it is."
        )
        return {
            "action_type": "autonomy_earnback",
            "action_category": category,
            "content": content,
            "rationale": rationale,
            "confidence": conf,
            "urgency": "low",
            "expected_outputs": {"target_level": target},
        }

    # -- Cell promotion (WS-8 PR-D) ----------------------------------------

    _CELL_PROMOTION_REJECT_COOLDOWN_DAYS = 7

    async def _check_cell_promotion_opportunities(self) -> None:
        """Propose promoting email capability cells whose approved competence now
        supports standing autonomy. User-ego only; evidence-gated; the user
        approves each promotion. Never auto-promotes.
        """
        if self._session._source_tag != "user_ego_cycle":
            return
        db = self._session._db
        if db is None:
            return
        try:
            from genesis.db.crud import capability_grants as cg
            from genesis.db.crud import ego as ego_crud

            candidates = await cg.detect_promotable_cells(db)
            if not candidates:
                return

            # Anti-spam: skip cells that already have a pending promotion proposal
            # or an active reject cooldown (the digest rate-limiter is disabled, so
            # these guards are the only backstop).
            pending = await ego_crud.list_pending_proposals(db)
            pending_cells = {
                p.get("action_category")
                for p in pending
                if p.get("action_type") == "cell_promotion"
            }

            to_make: list[dict] = []
            for cand in candidates:
                cell_id = cand["id"]
                if cell_id in pending_cells:
                    continue
                if await self._cell_promotion_in_cooldown(cell_id):
                    continue
                to_make.append(self._build_cell_promotion_proposal(cand))

            if not to_make:
                return

            batch_id, ids, _ = await self._session._proposals.create_batch(
                to_make,
                ego_source="user_ego_cycle",
            )
            if ids:
                await self._session._proposals.send_digest(
                    batch_id,
                    ego_source="user_ego_cycle",
                )
                logger.info(
                    "Cell promotion: proposed standing autonomy for %d cell(s)",
                    len(ids),
                )
        except Exception:
            logger.warning("Cell promotion opportunity check failed", exc_info=True)

    async def _cell_promotion_in_cooldown(self, cell_id: str) -> bool:
        """True if *cell_id*'s promotion was rejected within the cooldown window."""
        from genesis.db.crud import ego as ego_crud

        ts = await ego_crud.get_state(
            self._session._db,
            f"cell_promotion_reject:{cell_id}",
        )
        if not ts:
            return False
        try:
            rejected_at = datetime.fromisoformat(ts)
        except (ValueError, TypeError):
            return False
        if rejected_at.tzinfo is None:
            rejected_at = rejected_at.replace(tzinfo=UTC)
        age_days = (datetime.now(UTC) - rejected_at).days
        return age_days < self._CELL_PROMOTION_REJECT_COOLDOWN_DAYS

    @staticmethod
    def _build_cell_promotion_proposal(cand: dict) -> dict:
        """Build a proposal dict for a promotable capability cell."""
        cell_id = cand["id"]
        domain, verb, risk = cand["domain"], cand["verb"], cand["risk_class"]
        successes = cand.get("successes", 0)
        posterior = cand.get("posterior")
        conf = round(float(posterior), 2) if posterior is not None else 0.7
        content = (
            f"Promote the {cell_id} capability to standing autonomy. I've handled "
            f"this {successes} times with your approval (confidence {conf}); "
            f"promoting it means routine {risk} {domain} {verb}s won't need your "
            f"sign-off each time. The auto-revert net still applies — any harm "
            f"signal immediately drops it back to needing your approval."
        )
        rationale = (
            "Evidence-gated promotion: the cell's own approved-success history "
            "supports standing autonomy. Approve to grant; reject to keep holding "
            "each send for approval."
        )
        return {
            "action_type": "cell_promotion",
            "action_category": cell_id,
            "content": content,
            "rationale": rationale,
            "confidence": conf,
            "urgency": "low",
            "expected_outputs": {"cell": [domain, verb, risk]},
        }

    # -- Tick handlers -----------------------------------------------------

    async def _on_tick(self) -> None:
        """Interval trigger handler. Pushes idle signal for consumer loop.

        Gate checks run here (emission-time). The consumer loop re-checks
        under lock before running the cycle. Lock is NOT held here —
        _on_tick only pushes signals, no shared mutable state besides
        _proactive_cycle_count (serialized by APScheduler max_instances=1).
        """
        self._emit_heartbeat("tick")
        logger.debug(
            "Ego tick fired (current_interval=%dm, config_cadence=%dm)",
            self._current_interval,
            self._config.cadence_minutes,
        )
        if not self._should_run(skip_idle_check=False):
            return

        # Quiet-hours floor (PROACTIVE only): overnight, throttle proactive
        # ticks to at most one per quiet_hours_min_interval_minutes. Checked
        # before the deep-think increment so a suppressed tick consumes no
        # counter slot. Morning report / reactive / escalation are unaffected.
        if self._quiet_hours_suppresses_tick():
            logger.debug("Ego proactive tick suppressed — quiet hours")
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

        signal = EgoSignal(
            signal_type="timer",
            focus_category="proactive",
            summary=f"Idle tick #{self._proactive_cycle_count}",
            priority="medium",
            metadata={"model_override": model_override} if model_override else {},
            # Self-superseding: the next tick pushes a fresh one, so a
            # tick parked longer than the current interval is stale.
            expires_at=_expires_in(self._current_interval),
        )
        if self._signal_queue.push(signal):
            self._last_proactive_fire_at = _now_utc()
            logger.debug("Proactive signal pushed: %s", signal.summary)
        else:
            # Roll back count so deep-think alignment is preserved
            self._proactive_cycle_count -= 1

    async def _process_signals(self) -> None:
        """Drain signal queue and run unified cycle.

        Separated from the consumer loop for testability — tests call
        this directly after pushing signals.

        Reactive rate limiting happens here (not at push time) to preserve
        batch semantics: a burst of events batches into one cycle consuming
        one rate-limit slot.
        """
        signals = self._signal_queue.drain()
        if not signals:
            return

        # Reactive rate limit: max N reactive-focused cycles per hour.
        # If the batch contains reactive signals and the limit is hit,
        # drop reactive signals but keep non-reactive ones (e.g., a
        # proactive tick that landed in the same batch window).
        has_reactive = any(s.focus_category == "reactive" for s in signals)
        if has_reactive:
            now = datetime.now(UTC)
            cutoff = now - timedelta(hours=1)
            self._reactive_timestamps = [ts for ts in self._reactive_timestamps if ts > cutoff]
            if len(self._reactive_timestamps) >= self._reactive_max_per_hour:
                non_reactive = [s for s in signals if s.focus_category != "reactive"]
                if not non_reactive:
                    logger.info(
                        "Reactive rate limit: %d/%d in last hour, dropping %d signal(s)",
                        len(self._reactive_timestamps),
                        self._reactive_max_per_hour,
                        len(signals),
                    )
                    return
                signals = non_reactive
                has_reactive = False

        # Extract overrides from signal metadata (deep-think model, morning
        # effort, escalation effort). Use `is None` checks (not falsy) so
        # empty strings don't slip through.
        model_override = None
        effort_override = None
        for sig in signals:
            if model_override is None:
                mo = sig.metadata.get("model_override")
                if mo is not None:
                    model_override = mo
            if effort_override is None:
                eo = sig.metadata.get("effort_override")
                if eo is not None:
                    effort_override = eo

        async with self._lock:
            # Re-check gates under lock (state may have changed since emission).
            # If rejected, drained signals are lost — acceptable for timer ticks
            # since the next scheduled tick will push a new signal.
            if not self._should_run(skip_idle_check=True):
                return

            try:
                cycle = await self._session.run_unified_cycle(
                    signals,
                    model_override=model_override,
                    effort_override=effort_override,
                )
            except CycleBlockedError as exc:
                logger.info("Unified cycle gated: %s", exc)
                return
            except Exception as exc:
                logger.error("Unified cycle failed", exc_info=True)
                self._record_failure(str(exc))
                return

            if cycle is None:
                # None here means CC-level failure (session creation,
                # invocation error). The "no actionable signals" path
                # from _perceive cannot reach here because we guard
                # `if not signals: return` above.
                self._record_failure("unified cycle returned None")
                return

            # Validate output (same logic as the old _on_tick)
            proposals = []
            try:
                proposals = json.loads(cycle.proposals_json)
            except (json.JSONDecodeError, TypeError):
                logger.debug(
                    "Could not parse proposals_json for cycle %s",
                    cycle.id,
                )

            if not cycle.focus_summary and not proposals:
                logger.warning(
                    "Unified cycle %s produced no usable output",
                    cycle.id,
                )
                self._record_failure("cycle produced no usable output")
                return

            self._record_success()

            # Record reactive timestamp for rate limiting
            if has_reactive:
                self._reactive_timestamps.append(datetime.now(UTC))

            # Morning report always resets interval to base (it's a
            # reporting event, not a proposal-productivity measurement).
            is_morning_report = any(s.focus_category == "daily_briefing" for s in signals)
            await self._update_interval(
                had_proposals=bool(proposals) or is_morning_report,
            )

    async def _signal_consumer_loop(self) -> None:
        """Background consumer for the unified signal queue.

        Blocks until signals arrive, brief batch window, then processes.
        All cycle types (proactive, morning, reactive, escalation) flow
        through this single loop.
        """
        while True:
            try:
                await self._signal_queue.wait()
                await asyncio.sleep(2)  # Brief batch window
                await self._process_signals()
            except asyncio.CancelledError:
                logger.debug("Signal consumer loop cancelled")
                break
            except Exception:
                logger.warning(
                    "Signal consumer error — backing off 60s",
                    exc_info=True,
                )
                await asyncio.sleep(60)

    async def _on_morning_report(self) -> None:
        """Cron trigger handler. Pushes daily briefing signal.

        Lock is NOT held — same reasoning as _on_tick(). The consumer
        loop acquires the lock before running the unified cycle.
        """
        if not self._should_run(skip_idle_check=True):
            return

        from datetime import date

        signal = EgoSignal(
            signal_type="timer",
            focus_category="daily_briefing",
            summary=f"Morning report {date.today().isoformat()}",
            priority="high",
            metadata={
                # No model_override — uses config model (user-configurable).
                # Effort override comes from the dedicated config field.
                "effort_override": self._config.morning_report_effort,
            },
            # 6h: a briefing delivered same-morning (e.g. once a pending
            # approval resolves) is useful; by afternoon it is stale and
            # tomorrow's cron covers it.
            expires_at=_expires_in(6 * 60),
        )
        if self._signal_queue.push(signal):
            logger.info("Morning report signal pushed")

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

            # Per-ego job_health key (user_ego_cycle / genesis_ego_cycle) — the
            # two egos write to the ONE global job_health table, so a shared
            # "ego_cycle" key made them clobber each other's health row.
            GenesisRuntime.instance().record_job_success(self._session._source_tag)
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

            # Per-ego job_health key — see _record_success.
            GenesisRuntime.instance().record_job_failure(
                self._session._source_tag,
                error,
            )
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

    async def _compute_boot_first_fire(self) -> datetime | None:
        """Restart-safe first-fire time for the ``ego_cycle`` job.

        The proactive cadence uses an ``IntervalTrigger`` whose interval adapts
        (base → up to 72h) and RESETS to base on every restart, and APScheduler
        counts the interval from scheduler start with no ``next_run_time``. On a
        restart-heavy install this defers — and keeps re-deferring — the first
        proactive fire by a full backed-off interval (the documented
        IntervalTrigger-resets-on-restart trap).

        Anchor the boot first-fire to this ego's OWN ``job_health.last_success``
        (per-ego since #863; persisted and reloaded across restarts). Mirrors the
        ``memory_extraction`` boot-pin from #863: fire ~soon after boot when the
        ego is overdue, otherwise wait out only the remaining base interval so a
        restart does not re-run the expensive cycle prematurely.

        Returns the datetime for ``add_job(next_run_time=...)``, or ``None`` when
        there is no prior successful cycle (fresh install / no row / read error)
        so ``start()`` OMITS the kwarg and keeps APScheduler's default
        trigger-computed first fire. ``None`` must never be passed to
        ``add_job`` — an explicit ``next_run_time=None`` marks the job PAUSED.
        """
        try:
            from genesis.db.crud import job_health as job_health_crud

            last_success_iso = await job_health_crud.get_job_last_success(
                self._db,
                self._session._source_tag,
            )
        except Exception:
            logger.debug(
                "Boot first-fire: job_health read failed; using default timing",
                exc_info=True,
            )
            return None

        if not last_success_iso:
            return None
        try:
            last_success = datetime.fromisoformat(last_success_iso)
        except (ValueError, TypeError):
            return None
        if last_success.tzinfo is None:
            last_success = last_success.replace(tzinfo=UTC)

        now = datetime.now(UTC)
        # Anchor to the CURRENT (restored) interval, not base — a backed-off
        # ego resuming after a restart must not re-accelerate to base cadence.
        interval = timedelta(minutes=self._current_interval)
        # ~60s boot-pin mirrors #863's memory_extraction restart-safe schedule.
        boot_pin = now + timedelta(seconds=60)
        return max(boot_pin, last_success + interval)

    def _in_quiet_hours(self, now_local: datetime) -> bool:
        """True if *now_local* falls inside the configured quiet-hours window.

        Handles windows that cross midnight (e.g. 23 → 7). A window with
        start == end is treated as disabled (zero-width).
        """
        start = self._config.quiet_hours_start
        end = self._config.quiet_hours_end
        if start == end:
            return False
        hour = now_local.hour
        if start < end:
            return start <= hour < end
        return hour >= start or hour < end  # crosses midnight

    def _quiet_hours_suppresses_tick(self) -> bool:
        """Whether the current proactive tick should be skipped for quiet hours.

        Suppresses only when quiet hours are enabled, the local clock is inside
        the window, AND the last proactive fire was less than
        quiet_hours_min_interval_minutes ago. A first tick with no recorded
        prior fire is allowed (never block on unknown boot state).
        """
        if not getattr(self._config, "quiet_hours_enabled", False):
            return False
        if not self._in_quiet_hours(_local_now(user_timezone())):
            return False
        last = self._last_proactive_fire_at
        if last is None:
            return False
        floor = timedelta(minutes=self._config.quiet_hours_min_interval_minutes)
        return (_now_utc() - last) < floor

    async def _restore_interval(self) -> None:
        """Load the persisted adaptive interval, clamped to current config
        bounds. Fresh install / missing / unparseable → keep the base default.
        """
        try:
            from genesis.db.crud import ego as ego_crud

            raw = await ego_crud.get_state(
                self._db,
                f"cadence_interval:{self._session._source_tag}",
            )
        except Exception:
            logger.debug("Cadence interval restore read failed", exc_info=True)
            return
        if not raw:
            return
        try:
            value = int(raw)
        except (ValueError, TypeError):
            return
        lo = self._config.cadence_minutes
        hi = self._config.max_interval_minutes
        clamped = max(lo, min(value, hi))
        if clamped != self._current_interval:
            logger.info(
                "Ego interval restored: %dm (persisted=%s, clamped to [%d,%d])",
                clamped,
                raw,
                lo,
                hi,
            )
        self._current_interval = clamped

    async def _persist_interval(self, interval: int) -> None:
        """Persist the adaptive interval so a restart resumes the backed-off
        cadence instead of resetting to base. Best-effort — never break the
        cycle path on a write failure.
        """
        try:
            from genesis.db.crud import ego as ego_crud

            await ego_crud.set_state(
                self._db,
                key=f"cadence_interval:{self._session._source_tag}",
                value=str(interval),
            )
        except Exception:
            logger.debug("Failed to persist cadence interval", exc_info=True)

    async def _update_interval(self, *, had_proposals: bool) -> None:
        """Adjust cycle interval based on productivity and user recency.

        - Idle cycle (no proposals): multiply by backoff_multiplier
        - Productive cycle: reset to base cadence_minutes
        - Max interval adapts to user recency (longer absence → higher cap)
        - Hot-reloads config from disk so dashboard changes take effect
        """
        # Hot-reload config from disk (allows dashboard changes without restart)
        try:
            from genesis.ego.config import load_ego_config

            fresh = load_ego_config()
            if fresh.cadence_minutes != self._config.cadence_minutes:
                logger.info(
                    "Ego config hot-reload: cadence %d → %d",
                    self._config.cadence_minutes,
                    fresh.cadence_minutes,
                )
            self._config = fresh
        except Exception:
            logger.debug("Config hot-reload failed, using cached", exc_info=True)

        recency_max = await self._recency_max_interval()

        if had_proposals:
            new_interval = self._config.cadence_minutes
        else:
            new_interval = min(
                int(self._current_interval * self._config.backoff_multiplier),
                recency_max,
            )

        logger.info(
            "Ego interval calc: new=%dm, current=%dm, had_proposals=%s, recency_max=%dm",
            new_interval,
            self._current_interval,
            had_proposals,
            recency_max,
        )
        if new_interval != self._current_interval:
            old_interval = self._current_interval
            self._current_interval = new_interval
            await self._persist_interval(new_interval)
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

    async def _on_heartbeat(self) -> None:
        """Fixed-interval liveness pulse (every 5 min), decoupled from the
        proactive ``_on_tick`` work cadence.

        Registered as the ``ego_heartbeat`` APScheduler job in :meth:`start`.
        Pure liveness — emits regardless of pause/idle/circuit state so a
        paused or quiet ego is not mistaken for a dead one. The only thing it
        proves (and the only thing the heartbeat is for) is that the ego
        subsystem's scheduler is alive. Best-effort via :meth:`_emit_heartbeat`.
        """
        self._emit_heartbeat("alive")

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
                name=f"ego_heartbeat_{trigger}",
            )
        except Exception:
            # Best-effort, but never silent — a dark heartbeat with a live
            # scheduler is exactly the thing this method exists to prevent.
            logger.debug("Ego heartbeat emission failed", exc_info=True)
