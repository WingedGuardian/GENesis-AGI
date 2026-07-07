"""SchedulerContext — the structural interface extracted job bodies depend on.

Job functions in this package take the live ``SurplusScheduler`` as their
first argument but are typed against this Protocol, so the job modules never
import the scheduler (no import cycle) and state exactly which scheduler
attributes they read.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    import aiosqlite

    from genesis.memory.store import MemoryStore
    from genesis.observability.events import GenesisEventBus
    from genesis.recon.gatherer import ReconGatherer
    from genesis.routing.router import Router
    from genesis.surplus.brainstorm import BrainstormRunner
    from genesis.surplus.queue import SurplusQueue
    from genesis.surplus.types import TaskType


class SchedulerContext(Protocol):
    """The slice of ``SurplusScheduler`` that extracted job bodies read."""

    _db: aiosqlite.Connection
    _event_bus: GenesisEventBus | None
    _queue: SurplusQueue
    _brainstorm_runner: BrainstormRunner
    _clock: Callable[[], datetime]
    _enable_code_audits: bool
    _code_audit_hours: int
    _code_index_hours: int
    _j9_eval_batch_hours: int
    _model_eval_hours: int
    _maintenance_hours: int
    _analytical_hours: int
    _recon_gatherer: ReconGatherer | None
    _model_intelligence_job: Any
    _models_md_synthesis_job: Any
    _skill_security_scan_job: Any
    _github_discovery_job: Any
    _extraction_store: MemoryStore | None
    _extraction_router: Router | None
    _follow_up_dispatcher: Any

    async def _recently_completed(
        self, task_type: TaskType, cooldown_hours: int | float,
    ) -> bool: ...

    async def schedule_pipeline(self, pipeline_name: str) -> str | None: ...

    async def _alarm_db_integrity(self, detail: str) -> None: ...
