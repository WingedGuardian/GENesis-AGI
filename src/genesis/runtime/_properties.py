"""Read-only property accessors for ``GenesisRuntime``.

These getters were extracted out of ``_core.py`` to bring the file back
under the 600 LOC soft target. Each property is a one-line passthrough
to a private ``_foo`` attribute set in ``GenesisRuntime.__init__`` (or
populated later by a bootstrap step). They are pure read accessors with
zero side effects — no caching, no lazy init, no validation.

This is a pure mixin: no ``__init__``, no methods other than properties,
and no class-level state. ``GenesisRuntime`` inherits from it and the
properties resolve through the normal MRO. The mixin references ``self._foo``
which is always set by the time any property is accessed because
``GenesisRuntime.__init__`` runs first.

``is_bootstrapped`` and ``bootstrap_mode`` are intentionally NOT moved
here — they live next to ``bootstrap()`` in ``_core.py`` because they
describe lifecycle state, not subsystem references.

Pause-related properties (``paused``, ``pause_reason``, ``paused_since``)
live in ``_pause_state.py`` alongside the methods that mutate them.
"""

from __future__ import annotations

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


class _RuntimeProperties:
    """Mixin: read-only accessors for GenesisRuntime subsystem references."""

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
    def autonomous_dispatcher(self) -> object | None:
        return self._autonomous_dispatcher

    @property
    def autonomous_cli_approval_gate(self) -> object | None:
        """Public accessor for the CLI fallback approval gate.

        Exposed so the Telegram bridge can inject the gate into the
        handler context for inline-button callback resolution and bare
        approve/reject text handling in the Approvals topic.
        """
        return self._autonomous_cli_approval_gate

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
