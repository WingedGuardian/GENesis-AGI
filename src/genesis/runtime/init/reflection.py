"""Init function: _init_reflection."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from genesis.runtime._core import GenesisRuntime

logger = logging.getLogger("genesis.runtime")


async def init(rt: GenesisRuntime) -> None:
    """Initialize reflection scheduler, stability monitor, context gatherer, output router."""
    if rt._db is None or rt._cc_reflection_bridge is None:
        logger.warning(
            "Reflection skipped — missing prerequisites "
            "(db=%s, bridge=%s)",
            rt._db is not None,
            rt._cc_reflection_bridge is not None,
        )
        return

    try:
        from genesis.reflection.context_gatherer import ContextGatherer
        from genesis.reflection.output_router import OutputRouter
        from genesis.reflection.question_gate import QuestionGate
        from genesis.reflection.scheduler import ReflectionScheduler
        from genesis.reflection.stability import LearningStabilityMonitor

        context_gatherer = ContextGatherer()
        question_gate = QuestionGate()
        output_router = OutputRouter(
            observation_writer=rt._observation_writer,
            event_bus=rt._event_bus,
            surplus_queue=rt._surplus_queue,
            question_gate=question_gate,
            outreach_pipeline=getattr(rt, "_outreach_pipeline", None),
        )
        rt._stability_monitor = LearningStabilityMonitor(
            event_bus=rt._event_bus,
        )

        rt._cc_reflection_bridge.set_context_gatherer(context_gatherer)
        rt._cc_reflection_bridge.set_output_router(output_router)
        if hasattr(rt, "_context_assembler") and rt._context_assembler is not None:
            rt._cc_reflection_bridge.set_context_assembler(rt._context_assembler)
        rt._output_router = output_router

        rt._reflection_scheduler = ReflectionScheduler(
            bridge=rt._cc_reflection_bridge,
            stability_monitor=rt._stability_monitor,
            db=rt._db,
            event_bus=rt._event_bus,
        )
        await rt._reflection_scheduler.start()
        logger.info("Genesis reflection scheduler started")

    except ImportError:
        logger.warning("genesis.reflection not available")
    except Exception:
        logger.exception("Failed to initialize reflection")
