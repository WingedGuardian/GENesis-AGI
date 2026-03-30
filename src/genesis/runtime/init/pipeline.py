"""Init functions: _init_pipeline, _run_pipeline_cycle."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from genesis.runtime._core import GenesisRuntime

logger = logging.getLogger("genesis.runtime")


async def run_pipeline_cycle(rt: GenesisRuntime, profile_name: str) -> None:
    """Run a single pipeline cycle and record job health."""
    job_name = f"pipeline:{profile_name}"
    try:
        if rt._pipeline_orchestrator is None:
            return
        result = await rt._pipeline_orchestrator.run_cycle(profile_name)
        rt.record_job_success(job_name)
        logger.info(
            "Pipeline cycle %s: %d collected, %d survived, %d discarded",
            profile_name,
            result.tier0_collected,
            result.tier1_survived,
            result.discarded,
        )
    except Exception as exc:
        rt.record_job_failure(job_name, str(exc))
        logger.error("Pipeline cycle %s failed", profile_name, exc_info=True)


async def init(rt: GenesisRuntime) -> None:
    """Initialize PipelineOrchestrator with collectors, triage, and module dispatch."""
    try:
        from genesis.modules.dispatcher import ModuleDispatcher
        from genesis.pipeline.collectors import CollectorRegistry
        from genesis.pipeline.orchestrator import PipelineOrchestrator
        from genesis.pipeline.profiles import ProfileLoader
        from genesis.pipeline.triage import TriageFilter

        profile_loader = ProfileLoader()
        profile_loader.load_all()

        module_dispatcher = None
        if rt._module_registry is not None:
            module_dispatcher = ModuleDispatcher(rt._module_registry)

        rt._pipeline_orchestrator = PipelineOrchestrator(
            profile_loader=profile_loader,
            collector_registry=CollectorRegistry(),
            triage_filter=TriageFilter(),
            memory_store=rt._memory_store,
            router=rt._router,
            event_bus=rt._event_bus,
            module_dispatcher=module_dispatcher,
        )

        count = len(profile_loader.list_enabled())
        logger.info("Pipeline orchestrator initialized (%d enabled profiles)", count)

    except ImportError:
        logger.warning("genesis.pipeline not available")
    except Exception:
        logger.exception("Failed to initialize pipeline orchestrator")
