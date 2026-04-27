"""ReflectionEngine — orchestrates the perception pipeline."""

from __future__ import annotations

import logging

import aiosqlite

from genesis.awareness.types import Depth, TickResult
from genesis.observability.events import GenesisEventBus
from genesis.observability.types import Severity, Subsystem
from genesis.perception.caller import LLMCaller
from genesis.perception.context import ContextAssembler
from genesis.perception.parser import OutputParser
from genesis.perception.prompts import PromptBuilder
from genesis.perception.types import ReflectionResult
from genesis.perception.writer import ResultWriter

logger = logging.getLogger(__name__)

_MAX_RETRIES = 2
_CALL_SITE_MAP = {
    Depth.MICRO: "3_micro_reflection",
    Depth.LIGHT: "4_light_reflection",
}


class ReflectionEngine:
    """Orchestrates the perception pipeline.

    Stateless — all state lives in the DB and context assembly.
    Each call is independent.
    """

    def __init__(
        self,
        *,
        context_assembler: ContextAssembler,
        prompt_builder: PromptBuilder,
        llm_caller: LLMCaller,
        output_parser: OutputParser,
        result_writer: ResultWriter,
        event_bus: GenesisEventBus | None = None,
    ) -> None:
        self._assembler = context_assembler
        self._builder = prompt_builder
        self._caller = llm_caller
        self._parser = output_parser
        self._writer = result_writer
        self._event_bus = event_bus

    async def reflect(
        self,
        depth: Depth,
        tick: TickResult,
        *,
        db: aiosqlite.Connection,
        prior_context: str | None = None,
        call_site_override: str | None = None,
    ) -> ReflectionResult:
        depth_str = depth.value
        call_site = call_site_override or _CALL_SITE_MAP.get(depth)
        if call_site is None:
            return ReflectionResult(
                success=False,
                reason=f"depth_{depth_str}_not_implemented",
            )

        # 1. Assemble context
        context = await self._assembler.assemble(
            depth, tick, db=db, prior_context=prior_context,
        )

        # 2. Build prompt
        prompt = self._builder.build(depth_str, context)

        # 3. Call LLM
        response = await self._caller.call(prompt, call_site_id=call_site)
        if response is None:
            if self._event_bus:
                await self._event_bus.emit(
                    Subsystem.PERCEPTION,
                    Severity.WARNING,
                    "reflection.failed",
                    f"{depth_str} reflection failed: all providers exhausted",
                    depth=depth_str,
                    tick_id=tick.tick_id,
                )
            return ReflectionResult(
                success=False,
                reason="all_providers_exhausted",
            )

        # 4. Parse output (with retries)
        parsed = self._parser.parse(response, depth_str)
        retries = 0
        while not parsed.success and parsed.needs_retry and retries < _MAX_RETRIES:
            retries += 1
            logger.info(
                "Retrying %s reflection (attempt %d/%d)",
                depth_str, retries, _MAX_RETRIES,
            )
            retry_full = f"{prompt}\n\n---\n\n{parsed.retry_prompt}"
            response = await self._caller.call(
                retry_full, call_site_id=call_site,
            )
            if response is None:
                return ReflectionResult(
                    success=False,
                    reason="all_providers_exhausted",
                )
            parsed = self._parser.parse(response, depth_str)

        if not parsed.success:
            return ReflectionResult(
                success=False,
                reason="max_retries_exceeded",
            )

        # 5. Write results (returns False if gated by salience/dedup)
        stored = await self._writer.write(parsed.output, depth, tick, db=db)

        return ReflectionResult(success=True, output=parsed.output if stored else None)
