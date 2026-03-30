"""Init function: _init_perception."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from genesis.runtime._core import GenesisRuntime

logger = logging.getLogger("genesis.runtime")


def init(rt: GenesisRuntime) -> None:
    """Initialize the perception pipeline: ReflectionEngine, context assembler, LLM caller."""
    if rt._router is None:
        logger.warning("Router not available — skipping perception")
        return
    try:
        from genesis.identity.loader import IdentityLoader
        from genesis.perception.caller import LLMCaller
        from genesis.perception.context import ContextAssembler
        from genesis.perception.engine import ReflectionEngine
        from genesis.perception.parser import OutputParser
        from genesis.perception.prompts import PromptBuilder
        from genesis.perception.writer import ResultWriter

        identity_loader = IdentityLoader()
        rt._identity_loader = identity_loader
        context_assembler = ContextAssembler(identity_loader=identity_loader)
        rt._context_assembler = context_assembler
        prompt_builder = PromptBuilder()
        llm_caller = LLMCaller(router=rt._router, event_bus=rt._event_bus)
        output_parser = OutputParser()
        result_writer = ResultWriter(event_bus=rt._event_bus)
        rt._result_writer = result_writer

        rt._reflection_engine = ReflectionEngine(
            context_assembler=context_assembler,
            prompt_builder=prompt_builder,
            llm_caller=llm_caller,
            output_parser=output_parser,
            result_writer=result_writer,
            event_bus=rt._event_bus,
        )

        if rt._awareness_loop is not None:
            rt._awareness_loop.set_reflection_engine(rt._reflection_engine)
            logger.info("Reflection engine injected into awareness loop")

        logger.info("Genesis perception pipeline initialized")
    except ImportError:
        logger.warning("genesis.perception not available")
    except Exception:
        logger.exception("Failed to initialize perception")
