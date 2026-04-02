"""GenesisRuntime — singleton that owns all Genesis infrastructure.

Consolidates initialization from the six AZ agent_init extensions into a
single idempotent bootstrap.  The AZ ``server_startup`` extension calls
``GenesisRuntime.instance().bootstrap()`` once at server start; the
``agent_init`` extensions then just copy references to ``self.agent`` for
backward compatibility.

Every subsystem init is wrapped in try/except for graceful degradation —
a failure in one subsystem (e.g. Qdrant offline) must not prevent the rest
from starting.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import aiosqlite

    from genesis.awareness.loop import AwarenessLoop
    from genesis.cc.checkpoint import CheckpointManager
    from genesis.cc.context_injector import ContextInjector
    from genesis.cc.invoker import CCInvoker
    from genesis.cc.reflection_bridge import CCReflectionBridge
    from genesis.cc.session_manager import SessionManager
    from genesis.inbox.monitor import InboxMonitor
    from genesis.learning.pipeline import TriagePipeline
    from genesis.mail.monitor import MailMonitor
    from genesis.memory.retrieval import HybridRetriever
    from genesis.memory.store import MemoryStore
    from genesis.modules.registry import ModuleRegistry
    from genesis.observability import GenesisEventBus
    from genesis.observability.health_data import HealthDataService
    from genesis.observability.provider_activity import ProviderActivityTracker
    from genesis.outreach.pipeline import OutreachPipeline
    from genesis.outreach.scheduler import OutreachScheduler
    from genesis.providers.registry import ProviderRegistry
    from genesis.reflection.scheduler import ReflectionScheduler
    from genesis.reflection.stability import LearningStabilityMonitor
    from genesis.research.orchestrator import ResearchOrchestrator
    from genesis.resilience.cc_budget import CCBudgetTracker
    from genesis.resilience.deferred_work import DeferredWorkQueue
    from genesis.routing.circuit_breaker import CircuitBreakerRegistry
    from genesis.routing.cost_tracker import CostTracker
    from genesis.routing.dead_letter import DeadLetterQueue
    from genesis.routing.router import Router
    from genesis.surplus.idle_detector import IdleDetector
    from genesis.surplus.scheduler import SurplusScheduler

from genesis.runtime._job_health import (
    record_job_failure,
    record_job_success,
    register_channel,
)
from genesis.runtime.init import (
    autonomy,
    awareness,
    cc_relay,
    db,
    health_data,
    inbox,
    learning,
    mail,
    memory,
    modules,
    observability,
    outreach,
    perception,
    pipeline,
    providers,
    reflection,
    router,
    secrets,
    surplus,
    tasks,
)

logger = logging.getLogger("genesis.runtime")


async def record_init_degradation(
    db,
    event_bus,
    subsystem: str,
    component: str,
    error: str,
    *,
    severity: str = "warning",
) -> None:
    """Record a subsystem init degradation as an observation + event.

    Called from init modules when a non-critical component fails to wire.
    Creates an observation (visible in dashboard/morning report) and emits
    an event (visible in event bus/logs).
    """
    priority = "high" if severity == "error" else "medium"
    content_text = f"[{subsystem}] {component}: {error}"
    if db is not None:
        try:
            import uuid

            from genesis.db.crud import observations

            # Dedup: skip if an unresolved init_degradation for this component exists
            existing = await db.execute(
                "SELECT 1 FROM observations WHERE source = 'bootstrap' "
                "AND type = 'init_degradation' AND content = ? "
                "AND resolved_at IS NULL LIMIT 1",
                (content_text,),
            )
            if await existing.fetchone():
                logger.debug("Init degradation already recorded for %s.%s", subsystem, component)
            else:
                await observations.create(
                    db,
                    id=str(uuid.uuid4()),
                    source="bootstrap",
                    type="init_degradation",
                    content=content_text,
                    priority=priority,
                    created_at=datetime.now(UTC).isoformat(),
                    category="infrastructure",
                )
        except Exception:
            logger.warning("Failed to record init degradation observation", exc_info=True)
    if event_bus is not None:
        try:
            from genesis.observability.types import Severity, Subsystem

            sev = Severity.ERROR if severity == "error" else Severity.WARNING
            await event_bus.emit(
                Subsystem.INFRA,
                sev,
                f"init.{subsystem}.degraded",
                f"{component}: {error}",
            )
        except Exception:
            logger.warning("Failed to emit init degradation event", exc_info=True)


class GenesisRuntime:
    """Singleton that owns all Genesis infrastructure references."""

    _instance: GenesisRuntime | None = None

    @classmethod
    def instance(cls) -> GenesisRuntime:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Clear singleton state.  For tests only."""
        cls._instance = None

    _CRITICAL_SUBSYSTEMS: frozenset[str] = frozenset({"db", "observability", "router"})

    def __init__(self) -> None:
        self._bootstrapped = False

        self._db: aiosqlite.Connection | None = None
        self._event_bus: GenesisEventBus | None = None
        self._awareness_loop: AwarenessLoop | None = None
        self._router: Router | None = None
        self._reflection_engine: object | None = None
        self._cc_invoker: CCInvoker | None = None
        self._session_manager: SessionManager | None = None
        self._checkpoint_manager: CheckpointManager | None = None
        self._cc_reflection_bridge: CCReflectionBridge | None = None
        self._memory_store: MemoryStore | None = None
        self._observation_writer: object | None = None
        self._triage_pipeline: TriagePipeline | None = None
        self._learning_scheduler: object | None = None
        self._inbox_monitor: InboxMonitor | None = None
        self._mail_monitor: MailMonitor | None = None
        self._surplus_scheduler: SurplusScheduler | None = None
        self._reflection_scheduler: ReflectionScheduler | None = None
        self._stability_monitor: LearningStabilityMonitor | None = None
        self._provider_registry: ProviderRegistry | None = None
        self._module_registry: ModuleRegistry | None = None
        self._pipeline_orchestrator: object | None = None
        self._research_orchestrator: ResearchOrchestrator | None = None
        self._hybrid_retriever: HybridRetriever | None = None
        self._context_injector: ContextInjector | None = None
        self._circuit_breakers: CircuitBreakerRegistry | None = None
        self._cost_tracker: CostTracker | None = None
        self._dead_letter_queue: DeadLetterQueue | None = None
        self._deferred_work_queue: DeferredWorkQueue | None = None
        self._cc_budget_tracker: CCBudgetTracker | None = None
        self._health_data: HealthDataService | None = None
        self._activity_tracker: ProviderActivityTracker | None = None
        self._outreach_pipeline: OutreachPipeline | None = None
        self._outreach_scheduler: OutreachScheduler | None = None
        self._engagement_tracker = None
        self._autonomy_manager: object | None = None
        self._action_classifier: object | None = None
        self._task_verifier: object | None = None
        self._protected_paths: object | None = None
        self._resilience_state_machine: object | None = None
        self._status_writer: object | None = None
        self._recovery_orchestrator: object | None = None
        self._result_writer: object | None = None
        self._approval_manager: object | None = None
        self._approval_timeout_task: asyncio.Task | None = None
        self._findings_bridge: object | None = None
        self._idle_detector: IdleDetector | None = None
        self._surplus_queue: object | None = None
        self._model_profile_registry: object | None = None
        self._contingency_dispatcher: object | None = None
        self._prediction_logger: object | None = None
        self._identity_loader: object | None = None
        self._output_router: object | None = None
        self._task_executor: object | None = None
        self._task_dispatcher: object | None = None
        self._task_dispatch_poll: asyncio.Task | None = None

        # Global pause state — blocks all background dispatches when True.
        self._paused: bool = False
        self._pause_reason: str | None = None
        self._paused_since: datetime | None = None

        self._bootstrap_manifest: dict[str, str] = {}
        self._job_health: dict[str, dict] = {}
        self._job_retry_registry = None

    # ── Pause / kill switch ──────────────────────────────────

    _PAUSE_FILE = Path.home() / ".genesis" / "paused.json"

    @property
    def paused(self) -> bool:
        # Check file on disk so cross-process pause (dashboard → bridge) works.
        # Only reads file when in-memory state is unpaused (cheap fast path).
        if not self._paused and self._PAUSE_FILE.exists():
            self._restore_pause_state()
        elif self._paused and not self._PAUSE_FILE.exists():
            # Unpaused from another process (dashboard or Telegram)
            self._paused = False
            self._pause_reason = None
            self._paused_since = None
        return self._paused

    @property
    def pause_reason(self) -> str | None:
        return self._pause_reason

    @property
    def paused_since(self) -> datetime | None:
        return self._paused_since

    def set_paused(self, paused: bool, reason: str | None = None) -> None:
        self._paused = paused
        self._pause_reason = reason if paused else None
        self._paused_since = datetime.now(UTC) if paused else None
        self._persist_pause_state()
        logger.info("Genesis %s%s", "PAUSED" if paused else "RESUMED",
                     f" — {reason}" if reason else "")

    def _persist_pause_state(self) -> None:
        try:
            if self._paused:
                import json as _json

                self._PAUSE_FILE.parent.mkdir(parents=True, exist_ok=True)
                self._PAUSE_FILE.write_text(_json.dumps({
                    "paused": True,
                    "reason": self._pause_reason,
                    "since": self._paused_since.isoformat() if self._paused_since else None,
                }))
            else:
                self._PAUSE_FILE.unlink(missing_ok=True)
        except OSError:
            logger.warning("Failed to persist pause state", exc_info=True)

    def _restore_pause_state(self) -> None:
        try:
            if self._PAUSE_FILE.exists():
                import json as _json

                data = _json.loads(self._PAUSE_FILE.read_text())
                self._paused = data.get("paused", False)
                self._pause_reason = data.get("reason")
                since_raw = data.get("since")
                self._paused_since = datetime.fromisoformat(since_raw) if since_raw else None
                if self._paused:
                    logger.warning(
                        "Genesis starting in PAUSED state (since %s: %s)",
                        self._paused_since, self._pause_reason,
                    )
        except (OSError, ValueError):
            logger.warning("Failed to restore pause state", exc_info=True)

    # ── Core properties ───────────────────────────────────────

    @property
    def is_bootstrapped(self) -> bool:
        return self._bootstrapped

    @property
    def bootstrap_mode(self) -> str:
        return getattr(self, "_bootstrap_mode", "not_bootstrapped")

    @property
    def db(self) -> aiosqlite.Connection | None:
        return self._db

    @property
    def event_bus(self) -> GenesisEventBus | None:
        return self._event_bus

    @property
    def awareness_loop(self) -> AwarenessLoop | None:
        return self._awareness_loop

    @property
    def router(self) -> Router | None:
        return self._router

    @property
    def reflection_engine(self) -> object | None:
        return self._reflection_engine

    @property
    def cc_invoker(self) -> CCInvoker | None:
        return self._cc_invoker

    @property
    def session_manager(self) -> SessionManager | None:
        return self._session_manager

    @property
    def checkpoint_manager(self) -> CheckpointManager | None:
        return self._checkpoint_manager

    @property
    def cc_reflection_bridge(self) -> CCReflectionBridge | None:
        return self._cc_reflection_bridge

    @property
    def memory_store(self) -> MemoryStore | None:
        return self._memory_store

    @property
    def triage_pipeline(self) -> TriagePipeline | None:
        return self._triage_pipeline

    @property
    def learning_scheduler(self) -> object | None:
        return self._learning_scheduler

    @property
    def inbox_monitor(self) -> InboxMonitor | None:
        return self._inbox_monitor

    @property
    def surplus_scheduler(self) -> SurplusScheduler | None:
        return self._surplus_scheduler

    @property
    def reflection_scheduler(self) -> ReflectionScheduler | None:
        return self._reflection_scheduler

    @property
    def stability_monitor(self) -> LearningStabilityMonitor | None:
        return self._stability_monitor

    @property
    def task_executor(self) -> object | None:
        return self._task_executor

    @property
    def provider_registry(self) -> ProviderRegistry | None:
        return self._provider_registry

    @property
    def module_registry(self) -> ModuleRegistry | None:
        return self._module_registry

    @property
    def pipeline_orchestrator(self) -> object | None:
        return self._pipeline_orchestrator

    @property
    def research_orchestrator(self) -> ResearchOrchestrator | None:
        return self._research_orchestrator

    @property
    def hybrid_retriever(self) -> HybridRetriever | None:
        return self._hybrid_retriever

    @property
    def context_injector(self) -> ContextInjector | None:
        return self._context_injector

    @property
    def circuit_breakers(self) -> CircuitBreakerRegistry | None:
        return self._circuit_breakers

    @property
    def cost_tracker(self) -> CostTracker | None:
        return self._cost_tracker

    @property
    def dead_letter_queue(self) -> DeadLetterQueue | None:
        return self._dead_letter_queue

    @property
    def deferred_work_queue(self) -> DeferredWorkQueue | None:
        return self._deferred_work_queue

    @property
    def cc_budget_tracker(self) -> CCBudgetTracker | None:
        return self._cc_budget_tracker

    @property
    def health_data(self) -> HealthDataService | None:
        return self._health_data

    @property
    def outreach_pipeline(self) -> OutreachPipeline | None:
        return self._outreach_pipeline

    @property
    def outreach_scheduler(self) -> OutreachScheduler | None:
        return self._outreach_scheduler

    @property
    def engagement_tracker(self):
        return self._engagement_tracker

    @property
    def activity_tracker(self) -> ProviderActivityTracker | None:
        return self._activity_tracker

    @property
    def job_retry_registry(self):
        return self._job_retry_registry

    @property
    def autonomy_manager(self) -> object | None:
        return self._autonomy_manager

    @property
    def action_classifier(self) -> object | None:
        return self._action_classifier

    @property
    def task_verifier(self) -> object | None:
        return self._task_verifier

    @property
    def protected_paths(self) -> object | None:
        return self._protected_paths

    @property
    def resilience_state_machine(self) -> object | None:
        return self._resilience_state_machine

    @property
    def status_writer(self) -> object | None:
        return self._status_writer

    @property
    def recovery_orchestrator(self) -> object | None:
        return self._recovery_orchestrator

    @property
    def approval_manager(self) -> object | None:
        return self._approval_manager

    @property
    def idle_detector(self) -> IdleDetector | None:
        return self._idle_detector

    @property
    def findings_bridge(self) -> object | None:
        return self._findings_bridge

    @property
    def model_profile_registry(self) -> object | None:
        return self._model_profile_registry

    @property
    def contingency_dispatcher(self) -> object | None:
        return self._contingency_dispatcher

    @property
    def bootstrap_manifest(self) -> dict[str, str]:
        return dict(self._bootstrap_manifest)

    @property
    def job_health(self) -> dict[str, dict]:
        return dict(self._job_health)

    def record_job_success(self, job_name: str) -> None:
        record_job_success(self, job_name)

    def record_job_failure(self, job_name: str, error: str) -> None:
        record_job_failure(self, job_name, error)

    async def _load_persisted_job_health(self) -> None:
        if self._db is None:
            return
        try:
            import aiosqlite

            async with self._db.execute(
                "SELECT job_name, last_run, last_success, last_failure, "
                "last_error, consecutive_failures FROM job_health"
            ) as cur:
                for row in await cur.fetchall():
                    self._job_health[row[0]] = {
                        "last_run": row[1],
                        "last_success": row[2],
                        "last_failure": row[3],
                        "last_error": row[4],
                        "consecutive_failures": row[5],
                    }
            if self._job_health:
                logger.info("Loaded %d persisted job health entries", len(self._job_health))
        except aiosqlite.OperationalError as exc:
            if "no such table" in str(exc):
                logger.debug("job_health table not yet available — will be created on first write")
            else:
                logger.error("Failed to load persisted job health", exc_info=True)
        except Exception:
            logger.error("Failed to load persisted job health", exc_info=True)

    def _wire_job_retry_registry(self) -> None:
        try:
            from genesis.awareness.job_retry import JobRetryRegistry

            registry = JobRetryRegistry()

            if self._awareness_loop is not None:
                registry.register("awareness_tick", self._awareness_loop._on_tick)
            if self._surplus_scheduler is not None and hasattr(self._surplus_scheduler, "brainstorm_check"):
                registry.register("surplus_brainstorm", self._surplus_scheduler.brainstorm_check)
            if self._outreach_scheduler is not None and hasattr(self._outreach_scheduler, "_health_check_job"):
                registry.register("health_check", self._outreach_scheduler._health_check_job)

            self._job_retry_registry = registry
            registered = registry.list_registered()
            logger.info(
                "JobRetryRegistry wired with %d jobs: %s",
                len(registered), ", ".join(registered),
            )
        except Exception:
            logger.error("Failed to wire JobRetryRegistry", exc_info=True)

    def _persist_job_health(self, job_name: str, entry: dict, now: str) -> None:
        if self._db is None:
            return
        # Check for a running event loop BEFORE creating the coroutine.
        # Creating it eagerly (as argument to tracked_task) and then catching
        # RuntimeError leaks an unawaited coroutine → RuntimeWarning every 60s.
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            logger.debug("No event loop — job health for %s persisted in-memory only", job_name)
            return

        from genesis.util.tasks import tracked_task

        snapshot = dict(entry)

        async def _write() -> None:
            try:
                await self._db.execute(
                    """INSERT INTO job_health
                       (job_name, last_run, last_success, last_failure, last_error,
                        consecutive_failures, total_runs, total_successes,
                        total_failures, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                       ON CONFLICT(job_name) DO UPDATE SET
                           last_run = excluded.last_run,
                           last_success = COALESCE(excluded.last_success, last_success),
                           last_failure = COALESCE(excluded.last_failure, last_failure),
                           last_error = COALESCE(excluded.last_error, last_error),
                           consecutive_failures = excluded.consecutive_failures,
                           total_runs = total_runs + 1,
                           total_successes = total_successes + CASE WHEN excluded.last_success IS NOT NULL THEN 1 ELSE 0 END,
                           total_failures = total_failures + CASE WHEN excluded.last_failure IS NOT NULL THEN 1 ELSE 0 END,
                           updated_at = excluded.updated_at
                    """,
                    (
                        job_name,
                        snapshot.get("last_run"),
                        snapshot.get("last_success"),
                        snapshot.get("last_failure"),
                        snapshot.get("last_error"),
                        snapshot.get("consecutive_failures", 0),
                        1 if snapshot.get("last_success") else 0,
                        1 if snapshot.get("last_failure") else 0,
                        now,
                    ),
                )
                await self._db.commit()
            except sqlite3.Error:
                logger.error("DB error persisting job health for %s", job_name, exc_info=True)
            except Exception:
                logger.error("Failed to persist job health for %s", job_name, exc_info=True)

        try:
            tracked_task(_write(), name=f"persist-job-health-{job_name}")
        except Exception:
            logger.error("Failed to schedule job health persistence for %s", job_name, exc_info=True)

    def register_channel(self, name: str, adapter: object, *, recipient: str | None = None) -> None:
        register_channel(self, name, adapter, recipient=recipient)

    async def bootstrap(self, *, mode: str = "full") -> None:
        if self._bootstrapped:
            logger.info("GenesisRuntime already bootstrapped — skipping")
            return

        self._bootstrap_mode = mode
        _full = mode == "full"
        logger.info("GenesisRuntime bootstrap starting (mode=%s)", mode)

        self._run_init_step("secrets", self._load_secrets)
        self._restore_pause_state()

        await self._run_init_step_async("db", self._init_db)

        if self._db is None:
            logger.error("DB init failed — cannot continue bootstrap")
            return

        await self._run_init_step_async("tool_registry", self._init_tool_registry)

        self._run_init_step("observability", self._init_observability)

        if self._activity_tracker is not None:
            try:
                await self._activity_tracker.warm_from_db()
            except Exception:
                logger.debug("Activity tracker warm-up failed", exc_info=True)

        self._run_init_step("providers", self._init_providers)

        await self._run_init_step_async("modules", self._init_modules)

        if _full:
            await self._run_init_step_async("awareness", self._init_awareness)

        self._run_init_step("router", self._init_router)

        self._run_init_step("perception", self._init_perception)

        await self._run_init_step_async("cc_relay", self._init_cc_relay)

        await self._run_init_step_async("memory", self._init_memory)

        if _full:
            await self._run_init_step_async("pipeline", self._init_pipeline)

        if _full:
            await self._run_init_step_async("surplus", self._init_surplus)

        if _full:
            await self._run_init_step_async("learning", self._init_learning)

        if _full:
            await self._run_init_step_async("inbox", self._init_inbox)

        if _full:
            await self._run_init_step_async("mail", self._init_mail)

        if _full:
            await self._run_init_step_async("reflection", self._init_reflection)

        self._run_init_step("health_data", self._init_health_data)

        if _full:
            await self._run_init_step_async("outreach", self._init_outreach)

        self._run_init_step("autonomy", self._init_autonomy)
        if _full:
            await self._run_init_step_async("tasks", self._init_tasks)
        self._run_init_step("guardian", self._probe_guardian_status)

        if _full:
            await self._run_init_step_async(
                "guardian_monitoring", self._init_guardian_monitoring,
            )

        await self._load_persisted_job_health()

        self._wire_job_retry_registry()

        critical_ok = all(
            self._bootstrap_manifest.get(name) == "ok"
            for name in self._CRITICAL_SUBSYSTEMS
        )
        self._bootstrapped = critical_ok
        if not critical_ok:
            failed = [
                name for name in self._CRITICAL_SUBSYSTEMS
                if self._bootstrap_manifest.get(name) != "ok"
            ]
            logger.error("Bootstrap incomplete — critical subsystems failed: %s", failed)
        ok = sum(1 for v in self._bootstrap_manifest.values() if v == "ok")
        total = len(self._bootstrap_manifest)
        logger.info("GenesisRuntime bootstrap complete: %d/%d subsystems ok (bootstrapped=%s)", ok, total, critical_ok)
        for name, status in self._bootstrap_manifest.items():
            if status != "ok":
                logger.warning("Bootstrap: %s → %s", name, status)

        self._write_capabilities_file()

    _CAPABILITY_DESCRIPTIONS: dict[str, str] = {
        "secrets": "API key loader for external services (Gemini, Groq, Mistral, etc.)",
        "db": "SQLite database (60+ tables) — use db_schema MCP tool to discover tables and columns before querying",
        "tool_registry": "Registry of known tools available to CC sessions",
        "observability": "Event bus, structured logging, and provider activity tracking",
        "providers": "Provider registry — web search, STT, TTS, embeddings, health probes, research orchestrator",
        "awareness": "Awareness loop — periodic signal collection ticks driving system health and perception",
        "router": "LLM routing with circuit breakers, cost tracking, and dead-letter queue",
        "perception": "Reflection engine — observation creation, pattern detection, signal processing",
        "cc_relay": "Claude Code invoker, session manager, checkpoints, and reflection bridge",
        "memory": "Hybrid memory store — SQLite + Qdrant vector search for cross-session knowledge",
        "surplus": "Surplus compute scheduler — uses idle time for brainstorms and enrichment tasks",
        "learning": "Learning pipeline — triage, calibration, harvest, and procedural learning",
        "inbox": "Inbox monitor — watches ~/inbox/ for markdown files with URLs, evaluates them in background CC sessions",
        "mail": "Mail monitor — polls Gmail inbox weekly, two-layer triage (Gemini + CC), stores recon findings",
        "reflection": "Reflection scheduler — deep and light cognitive reflection cycles",
        "health_data": "Health data service — aggregates subsystem status for dashboard and MCP tools",
        "outreach": "Outreach pipeline + scheduler — morning reports, alerts, proactive Telegram messages",
        "autonomy": "Autonomy manager — task classification, protected paths, action verification, approval gates",
        "modules": "Capability module registry — domain-specific add-on modules (prediction markets, crypto ops)",
        "pipeline": "Pipeline orchestrator — signal collection, triage, and module dispatch cycles",
        "memory_extraction": "Periodic cross-session memory extraction — entities, decisions, relationships from conversation transcripts",
        "tasks": "Task executor — autonomous multi-step task execution with adversarial review, pause/resume/cancel",
        "guardian": "External host VM guardian — container health monitoring, diagnosis, and recovery",
        "guardian_monitoring": "Guardian bidirectional monitoring — detects stale Guardian heartbeat and auto-restarts via SSH",
    }

    def _write_capabilities_file(self) -> None:
        import json
        import os
        import tempfile

        capabilities: dict[str, dict[str, str]] = {}

        existing: dict[str, dict[str, str]] = {}
        if getattr(self, "_bootstrap_mode", "") == "readonly":
            cap_file = Path.home() / ".genesis" / "capabilities.json"
            if cap_file.exists():
                try:
                    existing = json.loads(cap_file.read_text())
                except (json.JSONDecodeError, OSError) as exc:
                    logger.warning("Failed to read existing capabilities.json: %s", exc)

        for name, raw_status in self._bootstrap_manifest.items():
            desc = self._CAPABILITY_DESCRIPTIONS.get(name, name)
            if raw_status == "ok":
                capabilities[name] = {"status": "active", "description": desc}
            elif raw_status == "degraded":
                capabilities[name] = {"status": "degraded", "description": desc}
            else:
                error_msg = raw_status.removeprefix("failed: ") if raw_status.startswith("failed:") else raw_status
                capabilities[name] = {
                    "status": "failed",
                    "description": desc,
                    "error": error_msg,
                }

        _module_descs = {
            "prediction_markets": "Prediction market analysis — calibration-driven forecasting, market scanning, Kelly position sizing",
            "crypto_ops": "Crypto token operations — narrative detection, launch monitoring, position health tracking",
            "content_pipeline": "Content pipeline — idea capture, weekly planning, voice-calibrated script drafting, multi-platform publishing, analytics feedback loop",
        }
        if self._module_registry:
            for mod_name in self._module_registry.list_modules():
                mod = self._module_registry.get(mod_name)
                if mod:
                    capabilities[f"module:{mod_name}"] = {
                        "status": "active" if mod.enabled else "disabled",
                        "description": _module_descs.get(mod_name, mod_name),
                    }

        if existing:
            for name, info in existing.items():
                if name not in capabilities or (
                    info.get("status") == "active"
                    and capabilities[name].get("status") == "degraded"
                ):
                    capabilities[name] = info

        cap_file = Path.home() / ".genesis" / "capabilities.json"
        cap_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd, tmp_path = tempfile.mkstemp(dir=cap_file.parent, suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(capabilities, f, indent=2)
                os.replace(tmp_path, cap_file)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(tmp_path)
                raise
            logger.info("Capabilities file written: %d entries", len(capabilities))
        except OSError:
            logger.error("Failed to write capabilities file", exc_info=True)

    async def shutdown(self) -> None:
        if not self._bootstrapped:
            return

        logger.info("GenesisRuntime shutdown starting")

        if self._approval_timeout_task is not None and not self._approval_timeout_task.done():
            self._approval_timeout_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._approval_timeout_task
            logger.info("Stopped approval timeout poller")

        for name, component in [
            ("outreach_scheduler", self._outreach_scheduler),
            ("reflection_scheduler", self._reflection_scheduler),
            ("inbox_monitor", self._inbox_monitor),
            ("mail_monitor", self._mail_monitor),
            ("surplus_scheduler", self._surplus_scheduler),
            ("learning_scheduler", self._learning_scheduler),
            ("awareness_loop", self._awareness_loop),
        ]:
            if component is None:
                continue
            try:
                if name == "learning_scheduler":
                    component.shutdown(wait=False)
                else:
                    await component.stop()
                logger.info("Stopped %s", name)
            except Exception:
                logger.exception("Failed to stop %s", name)

        if self._module_registry is not None:
            try:
                await self._module_registry.unload_all()
                logger.info("Module registry unloaded")
            except Exception:
                logger.exception("Failed to unload modules")

        if self._event_bus is not None:
            try:
                await self._event_bus.stop()
            except Exception:
                logger.exception("Failed to stop event bus")

        if self._db is not None:
            try:
                await self._db.close()
                logger.info("DB closed")
            except Exception:
                logger.exception("Failed to close DB")

        self._bootstrapped = False
        logger.info("GenesisRuntime shutdown complete")

    _INIT_CHECKS: dict[str, str | None] = {
        "secrets": None,
        "db": "_db",
        "tool_registry": None,
        "observability": "_event_bus",
        "providers": "_provider_registry",
        "awareness": "_awareness_loop",
        "router": "_router",
        "perception": "_reflection_engine",
        "cc_relay": "_cc_invoker",
        "memory": "_memory_store",
        "surplus": "_surplus_scheduler",
        "learning": "_learning_scheduler",
        "inbox": "_inbox_monitor",
        "mail": "_mail_monitor",
        "reflection": "_reflection_scheduler",
        "health_data": "_health_data",
        "outreach": "_outreach_scheduler",
        "autonomy": "_autonomy_manager",
        "modules": "_module_registry",
        "pipeline": "_pipeline_orchestrator",
    }

    def _run_init_step(self, name: str, func) -> None:
        try:
            func()
        except Exception as exc:
            self._bootstrap_manifest[name] = f"failed: {exc}"
            logger.exception("Bootstrap step %s failed", name)
            return
        attr = self._INIT_CHECKS.get(name)
        if attr and getattr(self, attr, None) is None:
            self._bootstrap_manifest[name] = "degraded"
        else:
            self._bootstrap_manifest[name] = "ok"

    async def _run_init_step_async(self, name: str, func) -> None:
        try:
            await func()
        except Exception as exc:
            self._bootstrap_manifest[name] = f"failed: {exc}"
            logger.exception("Bootstrap step %s failed", name)
            return
        attr = self._INIT_CHECKS.get(name)
        if attr and getattr(self, attr, None) is None:
            self._bootstrap_manifest[name] = "degraded"
        else:
            self._bootstrap_manifest[name] = "ok"

    def _load_secrets(self) -> None:
        secrets.load(self)

    async def _init_db(self) -> None:
        await db.init(self)

    async def _init_tool_registry(self) -> None:
        await db.init_tool_registry(self)

    def _init_observability(self) -> None:
        observability.init(self)

    def _init_providers(self) -> None:
        providers.init(self)

    async def _init_modules(self) -> None:
        await modules.init(self)

    async def _init_awareness(self) -> None:
        await awareness.init(self)

    def _init_router(self) -> None:
        router.init(self)

    def _init_perception(self) -> None:
        perception.init(self)

    async def _init_cc_relay(self) -> None:
        await cc_relay.init(self)

    async def _init_memory(self) -> None:
        await memory.init(self)

    async def _init_pipeline(self) -> None:
        await pipeline.init(self)

    async def _run_pipeline_cycle(self, profile_name: str) -> None:
        await pipeline.run_pipeline_cycle(self, profile_name)

    async def _init_surplus(self) -> None:
        await surplus.init(self)

    async def _init_learning(self) -> None:
        await learning.init(self)

    async def _init_reflection(self) -> None:
        await reflection.init(self)

    async def _init_inbox(self) -> None:
        await inbox.init(self)

    async def _init_mail(self) -> None:
        await mail.init(self)

    def _init_health_data(self) -> None:
        health_data.init(self)

    async def _init_outreach(self) -> None:
        await outreach.init(self)

    def _init_autonomy(self) -> None:
        autonomy.init(self)

    async def _init_tasks(self) -> None:
        await tasks.init(self)

    async def _init_guardian_monitoring(self) -> None:
        from genesis.runtime.init.guardian import init_guardian_monitoring
        await init_guardian_monitoring(self)

    def _probe_guardian_status(self) -> None:
        """Check if the Guardian is alive by reading its heartbeat file.

        The Guardian runs on the host VM, not inside the container.
        It writes ~/.genesis/guardian_heartbeat.json every check cycle.
        This probe always succeeds — Guardian is optional infrastructure.
        Actual staleness monitoring is handled by probe_guardian() in the
        health data infrastructure snapshot.
        """
        import json
        from datetime import UTC, datetime
        heartbeat_path = Path.home() / ".genesis" / "guardian_heartbeat.json"
        try:
            data = json.loads(heartbeat_path.read_text())
            ts_str = data.get("timestamp", "")
            if ts_str:
                staleness = (datetime.now(UTC) - datetime.fromisoformat(ts_str)).total_seconds()
                logger.info("Guardian heartbeat: %.0fs ago", staleness)
            else:
                logger.info("Guardian heartbeat file exists but missing timestamp")
        except FileNotFoundError:
            logger.info("Guardian heartbeat not found (Guardian may not be installed)")
        except (json.JSONDecodeError, ValueError, OSError) as exc:
            logger.info("Guardian heartbeat unreadable: %s", exc)
