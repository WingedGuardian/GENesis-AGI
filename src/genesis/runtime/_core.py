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
import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import aiosqlite

    from genesis.awareness.loop import AwarenessLoop
    from genesis.cc import AgentProvider
    from genesis.cc.checkpoint import CheckpointManager
    from genesis.cc.context_injector import ContextInjector
    from genesis.cc.reflection_bridge import CCReflectionBridge
    from genesis.cc.session_manager import SessionManager
    from genesis.db.connection import ReadConnectionPool
    from genesis.inbox.monitor import InboxMonitor
    from genesis.learning.pipeline import TriagePipeline
    from genesis.mail.monitor import MailMonitor
    from genesis.memory.retrieval import HybridRetriever
    from genesis.memory.store import MemoryStore
    from genesis.modules.registry import ModuleRegistry
    from genesis.observability import GenesisEventBus
    from genesis.observability.health_data import HealthDataService
    from genesis.observability.provider_activity import ProviderActivityTracker
    from genesis.observability.span_writer import SpanWriter
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

from genesis.runtime._capabilities import write_capabilities_file
from genesis.runtime._degradation import record_init_degradation
from genesis.runtime._init_delegates import _InitDelegatesMixin
from genesis.runtime._job_health import (
    load_persisted_job_health,
    persist_job_health,
    record_job_failure,
    record_job_start,
    record_job_success,
    register_channel,
)
from genesis.runtime._pause_state import _PauseStateMixin
from genesis.runtime._properties import _RuntimeProperties

__all__ = ["GenesisRuntime", "record_init_degradation"]

logger = logging.getLogger("genesis.runtime")


class GenesisRuntime(_RuntimeProperties, _PauseStateMixin, _InitDelegatesMixin):
    """Singleton that owns all Genesis infrastructure references."""

    _instance: GenesisRuntime | None = None

    @classmethod
    def instance(cls) -> GenesisRuntime:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def peek(cls) -> GenesisRuntime | None:
        """Return the current singleton or None without ever constructing one.

        Use this from read-only observability paths where lazy-constructing
        a blank singleton would mask real bootstrap failures elsewhere.
        :meth:`instance` is the correct constructor for production code;
        :meth:`peek` is the correct read for code that only wants to
        observe whether a runtime exists.
        """
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Clear singleton state.  For tests only.

        This is the sync fast-path for tests that never bootstrap the
        runtime. It does NOT close resources on the prior instance — if
        the singleton was bootstrapped (real DB connection, background
        tasks, schedulers), use :meth:`ashutdown` instead so those
        resources get torn down cleanly.
        """
        cls._instance = None

    @classmethod
    async def ashutdown(cls) -> None:
        """Async-safe singleton teardown for in-process restarts.

        Calls :meth:`shutdown` on the current instance (closes the DB,
        stops schedulers, cancels background tasks, unloads modules) then
        clears the singleton reference so ``instance()`` produces a
        fresh object on its next call. Prefer this over :meth:`reset`
        when the prior instance was bootstrapped.

        **Ordering matters.** :meth:`shutdown` is awaited *before*
        ``cls._instance`` is cleared. If the order were reversed, a
        coroutine calling :meth:`instance` while teardown was in flight
        would observe ``_instance is None`` and construct a brand-new
        runtime that silently races the dying one. Clearing the
        reference last means a concurrent :meth:`instance` call during
        teardown still sees the instance being torn down — degraded
        but consistent with the rest of the in-flight work — rather
        than a fresh unbootstrapped object.

        :meth:`shutdown` is wrapped in ``try/finally`` so the singleton
        reference is *always* cleared even when ``shutdown()`` raises.
        Otherwise a partially wired runtime could leave the class-level
        pointer dangling at a defunct instance, defeating the purpose
        of :meth:`reset` calls that follow.

        Primary use cases are in-process teardown where singleton reuse
        matters: pytest fixtures that exercise bootstrap across tests,
        REPL sessions, and hot-reload scenarios. Production process-exit
        paths (``channels/bridge.py``, ``hosting/standalone.py``,
        ``cc/terminal.py``) continue to call the instance
        ``shutdown()`` directly because the process is about to exit
        and the dangling class-level reference is moot.

        No-op when no singleton exists. Safe on non-bootstrapped
        singletons — :meth:`shutdown` returns early when
        ``_bootstrapped is False``, and the reference is still cleared.
        """
        if cls._instance is None:
            return
        inst = cls._instance
        try:
            await inst.shutdown()
        except Exception:
            # Log at ERROR per observability rules — a teardown failure
            # is an operational failure worth surfacing. ``shutdown()``
            # already logs per-subsystem failures internally; this
            # catches anything outside the subsystem loop (e.g., a bug
            # in the approval-timeout-task cancellation path).
            logger.error(
                "ashutdown: inst.shutdown() raised during teardown",
                exc_info=True,
            )
        finally:
            # Always clear the reference, even if shutdown() raised.
            # A dangling defunct singleton is a worse outcome than a
            # fresh one; the next instance() call will rebuild from
            # scratch.
            cls._instance = None

    _CRITICAL_SUBSYSTEMS: frozenset[str] = frozenset({"db", "observability", "router"})

    def __init__(self) -> None:
        self._bootstrapped = False

        self._db: aiosqlite.Connection | None = None
        # Dedicated read-only pool for recall's read stages (follow-up ac27b693);
        # wired in _init_memory, closed before self._db at shutdown.
        self._db_ro_pool: ReadConnectionPool | None = None
        self._event_bus: GenesisEventBus | None = None
        self._awareness_loop: AwarenessLoop | None = None
        self._router: Router | None = None
        self._reflection_engine: object | None = None
        self._cc_invoker: AgentProvider | None = None
        self._cc_fallback_probe_worker: object | None = None
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
        self._span_writer: SpanWriter | None = None
        self._outreach_pipeline: OutreachPipeline | None = None
        self._outreach_scheduler: OutreachScheduler | None = None
        self._engagement_tracker = None
        self._user_job_scheduler: object | None = None
        self._autonomy_manager: object | None = None
        self._action_classifier: object | None = None
        self._task_verifier: object | None = None
        self._protected_paths: object | None = None
        self._resilience_state_machine: object | None = None
        self._status_writer: object | None = None
        self._recovery_orchestrator: object | None = None
        self._result_writer: object | None = None
        self._approval_manager: object | None = None
        self._autonomous_cli_approval_gate: object | None = None
        self._autonomous_dispatcher: object | None = None
        self._autonomous_cli_policy_exporter: object | None = None
        self._approval_timeout_task: asyncio.Task | None = None
        self._status_writer_task: asyncio.Task | None = None
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
        self._build_lane: object | None = None
        self._build_lane_poll: asyncio.Task | None = None
        self._reflex_ingestor: object | None = None
        self._direct_session_runner: object | None = None
        self._direct_session_poll: asyncio.Task | None = None
        self._ego_session: object | None = None  # User ego (primary)
        self._ego_cadence_manager: object | None = None  # User ego cadence
        self._ego_proposal_workflow: object | None = None
        self._genesis_ego_session: object | None = None  # Genesis ego (COO)
        self._genesis_ego_cadence_manager: object | None = None
        self._campaign_runner: object | None = None

        # Global pause state — blocks all background dispatches when True.
        self._paused: bool = False
        self._pause_reason: str | None = None
        self._paused_since: datetime | None = None

        # Heavy workload flag — set by long-running batch jobs (e.g. dream
        # cycle) so Sentinel and watchdog defer restart-type remediation.
        self._heavy_workload: str | None = None
        self._heavy_workload_since: datetime | None = None

        # Timestamp when bootstrap completed — used for grace periods.
        self._bootstrap_completed_at: datetime | None = None

        self._bootstrap_manifest: dict[str, str] = {}
        self._job_health: dict[str, dict] = {}
        self._job_retry_registry = None

    # ── Core properties ───────────────────────────────────────

    @property
    def is_bootstrapped(self) -> bool:
        return self._bootstrapped

    @property
    def bootstrap_mode(self) -> str:
        return getattr(self, "_bootstrap_mode", "not_bootstrapped")

    @property
    def heavy_workload(self) -> str | None:
        """Name of the currently running heavy workload, or None.

        Auto-expires after 2 hours to prevent permanent flag from a hung
        process (e.g. dream cycle blocked on unresponsive Qdrant).
        """
        if self._heavy_workload and self._heavy_workload_since:
            age = (datetime.now(UTC) - self._heavy_workload_since).total_seconds()
            if age > 7200:  # 2 hours — no legitimate batch job runs longer
                logger.warning(
                    "Heavy workload '%s' expired after %.0fs — auto-clearing",
                    self._heavy_workload,
                    age,
                )
                self._heavy_workload = None
                self._heavy_workload_since = None
        return self._heavy_workload

    def record_job_start(self, job_name: str) -> None:
        record_job_start(self, job_name)

    def record_job_success(self, job_name: str) -> None:
        record_job_success(self, job_name)

    def record_job_failure(
        self, job_name: str, error: str, *, error_type: str | None = None
    ) -> None:
        record_job_failure(self, job_name, error, error_type=error_type)

    async def _load_persisted_job_health(self) -> None:
        # Thin wrapper: preserves the class-level patch surface for tests
        # that mock this method via patch.object / patch.GenesisRuntime._*
        await load_persisted_job_health(self)

    def _wire_job_retry_registry(self) -> None:
        try:
            from genesis.awareness.job_retry import JobRetryRegistry

            registry = JobRetryRegistry()

            if self._awareness_loop is not None:
                registry.register("awareness_tick", self._awareness_loop._on_tick)
            if self._surplus_scheduler is not None and hasattr(
                self._surplus_scheduler, "brainstorm_check"
            ):
                registry.register("surplus_brainstorm", self._surplus_scheduler.brainstorm_check)
            if self._outreach_scheduler is not None and hasattr(
                self._outreach_scheduler, "_health_check_job"
            ):
                registry.register("health_check", self._outreach_scheduler._health_check_job)

            self._job_retry_registry = registry
            registered = registry.list_registered()
            logger.info(
                "JobRetryRegistry wired with %d jobs: %s",
                len(registered),
                ", ".join(registered),
            )
        except Exception:
            logger.error("Failed to wire JobRetryRegistry", exc_info=True)

    def _persist_job_health(self, job_name: str, entry: dict, now: str) -> None:
        # Thin wrapper: preserves the class-level patch surface for tests
        # that mock this method via patch("genesis.runtime.GenesisRuntime._persist_job_health").
        persist_job_health(self, job_name, entry, now)

    def register_channel(self, name: str, adapter: object, *, recipient: str | None = None) -> None:
        register_channel(self, name, adapter, recipient=recipient)

    async def bootstrap(self, *, mode: str = "full") -> None:
        if self._bootstrapped:
            logger.info("GenesisRuntime already bootstrapped — skipping")
            return

        self._bootstrap_mode = mode
        _full = mode == "full"
        logger.info("GenesisRuntime bootstrap starting (mode=%s)", mode)

        # WS-3: the runtime process must NEVER carry a session-origin env var —
        # the same memory MCP tool functions run in-process here (dashboard
        # tool_api, runtime memory init), and a stale GENESIS_SESSION_ORIGIN
        # inherited from a dev shell would silently stamp every in-process
        # write. Dispatched CC children get theirs from CCInvoker._build_env.
        os.environ.pop("GENESIS_SESSION_ORIGIN", None)

        # Validate + restore corrupt credential files BEFORE loading secrets,
        # so a zeroed/corrupt secrets.env is healed before load_dotenv reads it.
        self._run_init_step(
            "cred_integrity_startup",
            self._selfheal_credentials_startup,
        )

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

        if _full:
            self._run_init_step("cred_integrity", self._init_cred_integrity)

        if _full:
            self._run_init_step("alert_drain", self._init_alert_drain)

        self._run_init_step("router", self._init_router)

        self._run_init_step("perception", self._init_perception)

        await self._run_init_step_async("cc_relay", self._init_cc_relay)

        if _full:
            await self._run_init_step_async(
                "direct_session",
                self._init_direct_session,
            )

        await self._run_init_step_async("memory", self._init_memory)

        # Recover knowledge uploads stuck in 'processing' from prior crash
        if self._db is not None:
            try:
                from genesis.knowledge.ingest_upload import recover_stale_processing

                recovered = await recover_stale_processing()
                if recovered:
                    logger.info("Recovered %d stale knowledge uploads", recovered)
            except Exception:
                logger.warning("Knowledge upload recovery failed", exc_info=True)

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

        if _full:
            await self._run_init_step_async("campaigns", self._init_campaigns)

        if _full:
            await self._run_init_step_async("user_jobs", self._init_user_jobs)

        await self._run_init_step_async("autonomy", self._init_autonomy)

        if _full:
            await self._run_init_step_async("ego", self._init_ego)

        if _full:
            await self._run_init_step_async("tasks", self._init_tasks)

        if _full:
            await self._run_init_step_async("reflex", self._init_reflex)
        self._run_init_step("guardian", self._probe_guardian_status)

        if _full:
            await self._run_init_step_async(
                "guardian_monitoring",
                self._init_guardian_monitoring,
            )

        # After guardian_monitoring so rt._guardian_remote exists for the
        # host plane; non-blocking (spawns a delayed background refresh).
        if _full:
            await self._run_init_step_async(
                "infra_profile",
                self._init_infra_profile,
            )

        if _full:
            await self._run_init_step_async("sentinel", self._init_sentinel)

        await self._load_persisted_job_health()

        # Clear stale failure counts from pre-restart failures so the
        # dashboard starts clean after a code fix + deploy.
        from genesis.runtime._job_health import clear_stale_job_failures

        cleared = clear_stale_job_failures(self)
        if cleared:
            logger.info("Cleared stale failures from %d jobs on startup", cleared)

        # Expire stale cognitive state entries (TTL-based + legacy NULL cleanup)
        if self._db is not None:
            try:
                from genesis.db.crud import cognitive_state

                expired = await cognitive_state.expire_old(self._db)
                if expired:
                    logger.info("Expired %d stale cognitive state entries", expired)
            except Exception:
                logger.warning("Failed to expire cognitive state entries", exc_info=True)

        # Boot-time stale-session sweep — 'active' rows orphaned by a crashed
        # process shouldn't wait up to ~6h for the session_reaper cron.
        # Deliberately HERE, after ALL init steps: session end-hooks (e.g. the
        # ego dispatch-outcome tracker, registered in ego init) must exist
        # before orphans are expired, and ego init runs after learning init
        # (where the job itself is registered).
        if self._learning_scheduler is not None:
            try:
                _reaper_job = self._learning_scheduler.get_job("session_reaper")
                if _reaper_job is not None:
                    from genesis.util.tasks import tracked_task

                    tracked_task(
                        _reaper_job.func(),
                        name="initial_session_reap",
                    )
            except Exception:
                logger.warning("Boot session-reap kick failed", exc_info=True)

        # Data migrations (WS-C): once-per-install backfills of NON-schema state
        # (Qdrant payloads, entity graphs). POST-boot + background — unlike schema
        # migrations these must NEVER abort startup, and can be long-running.
        # tracked_task (fire-and-forget) mirrors the session-reap kick above. The
        # runner is self-guarding (never raises) and idempotent; requires_operator
        # migrations are skipped here and wait for a deliberate trigger.
        if self._db is not None:
            try:
                from genesis.db.data_migrations.runner import run_data_migrations
                from genesis.util.tasks import tracked_task

                tracked_task(
                    run_data_migrations(self._db),
                    name="data_migrations",
                )
            except Exception:
                logger.warning("Boot data-migration kick failed", exc_info=True)

        self._wire_job_retry_registry()

        critical_ok = all(
            self._bootstrap_manifest.get(name) == "ok" for name in self._CRITICAL_SUBSYSTEMS
        )
        self._bootstrapped = critical_ok
        if not critical_ok:
            failed = [
                name
                for name in self._CRITICAL_SUBSYSTEMS
                if self._bootstrap_manifest.get(name) != "ok"
            ]
            logger.error("Bootstrap incomplete — critical subsystems failed: %s", failed)
        if critical_ok:
            self._bootstrap_completed_at = datetime.now(UTC)
        ok = sum(1 for v in self._bootstrap_manifest.values() if v == "ok")
        total = len(self._bootstrap_manifest)
        logger.info(
            "GenesisRuntime bootstrap complete: %d/%d subsystems ok (bootstrapped=%s)",
            ok,
            total,
            critical_ok,
        )
        for name, status in self._bootstrap_manifest.items():
            if status != "ok":
                logger.warning("Bootstrap: %s → %s", name, status)

        write_capabilities_file(self)

    async def stop_outbound_senders(self) -> None:
        """Stop the outreach recovery worker before the Telegram client closes.

        The recovery worker is the one background sender with the
        retry-exhaust-then-permanent-discard pathology: it polls every 60s and
        burns its 5-retry budget against whatever client it's given. The host
        calls this BEFORE ``adapter.stop()`` closes the httpx client, so a late
        retry can't fire through a torn-down client and discard the payload —
        the failure that silently dropped two Sentinel approval requests on
        2026-07-15. Other outbound senders (schedulers, awareness) are stopped
        later in ``shutdown()``; a send from one in the small post-close window
        just re-raises (heal is disabled during shutdown) rather than
        exhaust-discarding. Idempotent and exception-guarded so it never blocks
        shutdown; ``shutdown()`` also stops the worker as a belt.
        """
        worker = getattr(self, "_outreach_recovery_worker", None)
        if worker is not None:
            try:
                await worker.stop()
            except Exception:
                logger.exception("Failed to stop outreach recovery worker")

    async def shutdown(self) -> None:
        if not self._bootstrapped:
            return

        logger.info("GenesisRuntime shutdown starting")

        if self._approval_timeout_task is not None and not self._approval_timeout_task.done():
            self._approval_timeout_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._approval_timeout_task
            logger.info("Stopped approval timeout poller")

        if self._status_writer_task is not None and not self._status_writer_task.done():
            self._status_writer_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._status_writer_task
            logger.info("Stopped status writer loop")

        # Cancel-and-await in-flight direct sessions while the DB is still
        # open, so _run_session's CancelledError handler can persist a
        # terminal 'failed' status (without this the rows linger 'active'
        # across every systemctl restart).
        if self._direct_session_runner is not None:
            try:
                stopped = await self._direct_session_runner.shutdown()
                if stopped:
                    logger.info(
                        "Cancelled %d in-flight direct sessions",
                        stopped,
                    )
            except Exception:
                logger.exception("Failed to stop direct-session runner")

        for name, component in [
            ("user_ego_cadence", self._ego_cadence_manager),
            ("genesis_ego_cadence", self._genesis_ego_cadence_manager),
            ("outreach_scheduler", self._outreach_scheduler),
            # getattr: only set when outreach init succeeded (init/outreach.py).
            ("outreach_recovery", getattr(self, "_outreach_recovery_worker", None)),
            ("user_job_scheduler", self._user_job_scheduler),
            ("reflection_scheduler", self._reflection_scheduler),
            ("inbox_monitor", self._inbox_monitor),
            ("mail_monitor", self._mail_monitor),
            ("surplus_scheduler", self._surplus_scheduler),
            ("learning_scheduler", self._learning_scheduler),
            ("campaign_runner", self._campaign_runner),
            ("awareness_loop", self._awareness_loop),
            ("cc_fallback_probe", self._cc_fallback_probe_worker),
            ("reflex_ingestor", self._reflex_ingestor),
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

        if self._hybrid_retriever is not None:
            reranker = getattr(self._hybrid_retriever, "_reranker", None)
            if reranker is not None:
                try:
                    await reranker.close()
                    logger.info("Closed Voyage reranker HTTP client")
                except Exception:
                    logger.debug("Reranker close failed", exc_info=True)

        try:
            from genesis.knowledge.pdf_extract import shutdown_pdf_pool

            await shutdown_pdf_pool()
            logger.info("PDF extraction pool shut down")
        except Exception:
            logger.exception("Failed to shut down PDF extraction pool")

        if self._event_bus is not None:
            try:
                await self._event_bus.stop()
            except Exception:
                logger.exception("Failed to stop event bus")

        # Close the recall read-only pool BEFORE the writer — readers stop
        # first (follow-up ac27b693). Idempotent; a failed memory init leaves
        # this None, so the guard also covers the pool-never-built case.
        if self._db_ro_pool is not None:
            try:
                await self._db_ro_pool.close()
                logger.info("Recall read-only pool closed")
            except Exception:
                logger.exception("Failed to close recall read-only pool")

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
        "user_jobs": "_user_job_scheduler",
        "autonomy": "_autonomy_manager",
        "modules": "_module_registry",
        "pipeline": "_pipeline_orchestrator",
        "campaigns": "_campaign_runner",
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
