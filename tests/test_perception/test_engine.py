"""Tests for ReflectionEngine — orchestrates the perception pipeline."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from genesis.awareness.types import Depth, SignalReading, TickResult
from genesis.perception.parser import ParseResult
from genesis.perception.types import LLMResponse, MicroOutput, PromptContext


def _make_tick(depth=Depth.MICRO) -> TickResult:
    return TickResult(
        tick_id="tick-1",
        timestamp="2026-03-05T10:00:00+00:00",
        source="scheduled",
        signals=[
            SignalReading(name="cpu", value=0.3, source="system",
                          collected_at="2026-03-05T10:00:00+00:00"),
        ],
        scores=[],
        classified_depth=depth,
        trigger_reason="threshold_exceeded",
    )


def _micro_output():
    return MicroOutput(
        tags=["idle"], salience=0.1, anomaly=False,
        summary="Normal.", signals_examined=1,
    )


async def test_reflect_success(db):
    from genesis.perception.engine import ReflectionEngine

    assembler = AsyncMock()
    assembler.assemble = AsyncMock(return_value=PromptContext(
        depth="Micro", identity="Genesis", signals_text="cpu: 0.3",
        tick_number=1,
    ))
    builder = MagicMock()
    builder.build = MagicMock(return_value="Test prompt")
    caller = AsyncMock()
    caller.call = AsyncMock(return_value=LLMResponse(
        text='{"tags":["idle"],"salience":0.1,"anomaly":false,"summary":"Normal.","signals_examined":1}',
        model="groq-free", input_tokens=100, output_tokens=50,
        cost_usd=0.0, latency_ms=200,
    ))
    parser = MagicMock()
    parser.parse = MagicMock(return_value=ParseResult(
        success=True, output=_micro_output(),
    ))
    writer = AsyncMock()
    writer.write = AsyncMock()

    engine = ReflectionEngine(
        context_assembler=assembler,
        prompt_builder=builder,
        llm_caller=caller,
        output_parser=parser,
        result_writer=writer,
    )

    result = await engine.reflect(Depth.MICRO, _make_tick(), db=db)

    assert result.success is True
    assert result.output is not None
    assembler.assemble.assert_called_once()
    builder.build.assert_called_once()
    caller.call.assert_called_once()
    parser.parse.assert_called_once()
    writer.write.assert_called_once()


async def test_reflect_llm_failure_returns_failed_result(db):
    from genesis.perception.engine import ReflectionEngine

    assembler = AsyncMock()
    assembler.assemble = AsyncMock(return_value=PromptContext(
        depth="Micro", identity="Genesis", signals_text="cpu: 0.3",
        tick_number=1,
    ))
    builder = MagicMock()
    builder.build = MagicMock(return_value="Test prompt")
    caller = AsyncMock()
    caller.call = AsyncMock(return_value=None)
    parser = MagicMock()
    writer = AsyncMock()

    engine = ReflectionEngine(
        context_assembler=assembler,
        prompt_builder=builder,
        llm_caller=caller,
        output_parser=parser,
        result_writer=writer,
    )

    result = await engine.reflect(Depth.MICRO, _make_tick(), db=db)

    assert result.success is False
    assert result.reason == "all_providers_exhausted"
    writer.write.assert_not_called()


async def test_reflect_retry_on_parse_failure(db):
    from genesis.perception.engine import ReflectionEngine

    assembler = AsyncMock()
    assembler.assemble = AsyncMock(return_value=PromptContext(
        depth="Micro", identity="Genesis", signals_text="cpu: 0.3",
        tick_number=1,
    ))
    builder = MagicMock()
    builder.build = MagicMock(return_value="Test prompt")

    response = LLMResponse(
        text="bad json", model="groq-free", input_tokens=100,
        output_tokens=50, cost_usd=0.0, latency_ms=200,
    )
    good_response = LLMResponse(
        text='{"tags":["idle"],"salience":0.1,"anomaly":false,"summary":"Normal.","signals_examined":1}',
        model="groq-free", input_tokens=100, output_tokens=50,
        cost_usd=0.0, latency_ms=200,
    )
    caller = AsyncMock()
    caller.call = AsyncMock(side_effect=[response, good_response])

    parser = MagicMock()
    parser.parse = MagicMock(side_effect=[
        ParseResult(success=False, needs_retry=True, retry_prompt="Fix JSON"),
        ParseResult(success=True, output=_micro_output()),
    ])
    writer = AsyncMock()
    writer.write = AsyncMock()

    engine = ReflectionEngine(
        context_assembler=assembler,
        prompt_builder=builder,
        llm_caller=caller,
        output_parser=parser,
        result_writer=writer,
    )

    result = await engine.reflect(Depth.MICRO, _make_tick(), db=db)

    assert result.success is True
    assert caller.call.call_count == 2


async def test_reflect_max_retries_exceeded(db):
    from genesis.perception.engine import ReflectionEngine

    assembler = AsyncMock()
    assembler.assemble = AsyncMock(return_value=PromptContext(
        depth="Micro", identity="Genesis", signals_text="cpu: 0.3",
        tick_number=1,
    ))
    builder = MagicMock()
    builder.build = MagicMock(return_value="Test prompt")

    bad_response = LLMResponse(
        text="bad", model="groq-free", input_tokens=100,
        output_tokens=50, cost_usd=0.0, latency_ms=200,
    )
    caller = AsyncMock()
    caller.call = AsyncMock(return_value=bad_response)

    parser = MagicMock()
    parser.parse = MagicMock(return_value=ParseResult(
        success=False, needs_retry=True, retry_prompt="Fix JSON",
    ))
    writer = AsyncMock()

    engine = ReflectionEngine(
        context_assembler=assembler,
        prompt_builder=builder,
        llm_caller=caller,
        output_parser=parser,
        result_writer=writer,
    )

    result = await engine.reflect(Depth.MICRO, _make_tick(), db=db)

    assert result.success is False
    assert result.reason == "max_retries_exceeded"
    assert caller.call.call_count == 3  # original + 2 retries
    writer.write.assert_not_called()
