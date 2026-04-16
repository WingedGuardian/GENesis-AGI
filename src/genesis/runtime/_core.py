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
from datetime import datetime
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

from genesis.runtime._capabilities import write_capabilities_file
from genesis.runtime._degradation import record_init_degradation
from genesis.runtime._init_delegates import _InitDelegatesMixin
from genesis.runtime._job_health import (
    load_persisted_job_health,
    persist_job_health,
    record_job_failure,
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

        # Global pause state — blocks all background dispatches when True.
        self._paused: bool = False
        self._pause_reason: str | None = None
        self._paused_since: datetime | None = None

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

    def record_job_success(self, job_name: str) -> None:
        record_job_success(self, job_name)

    def record_job_failure(self, job_name: str, error: str) -> None:
        record_job_failure(self, job_name, error)

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

        await self._run_init_step_async("autonomy", self._init_autonomy)
        if _full:
            await self._run_init_step_async("tasks", self._init_tasks)
        self._run_init_step("guardian", self._probe_guardian_status)

        if _full:
            await self._run_init_step_async(
                "guardian_monitoring", self._init_guardian_monitoring,
            )

        if _full:
            await self._run_init_step_async("sentinel", self._init_sentinel)

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

        write_capabilities_file(self)

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

