"""Tests for StubExecutor and SurplusLLMExecutor."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from genesis.surplus.executor import StubExecutor, SurplusLLMExecutor
from genesis.surplus.types import ComputeTier, SurplusTask, TaskStatus, TaskType


def _make_task(task_type=TaskType.BRAINSTORM_USER, **kwargs):
    defaults = dict(
        id="st-1",
        task_type=task_type,
        compute_tier=ComputeTier.FREE_API,
        priority=0.5,
        drive_alignment="curiosity",
        status=TaskStatus.RUNNING,
        created_at="2026-03-04T10:00:00+00:00",
    )
    defaults.update(kwargs)
    return SurplusTask(**defaults)


@pytest.mark.asyncio
async def test_returns_success():
    result = await StubExecutor().execute(_make_task())
    assert result.success is True


@pytest.mark.asyncio
async def test_content_is_placeholder():
    result = await StubExecutor().execute(_make_task())
    assert "placeholder" in result.content.lower() or "stub" in result.content.lower()


@pytest.mark.asyncio
async def test_includes_task_type_in_content():
    task = _make_task(task_type=TaskType.BRAINSTORM_SELF)
    result = await StubExecutor().execute(task)
    assert "brainstorm_self" in result.content


@pytest.mark.asyncio
async def test_insights_list_populated():
    result = await StubExecutor().execute(_make_task())
    assert len(result.insights) >= 1
    for insight in result.insights:
        assert "content" in insight
        assert "drive_alignment" in insight


@pytest.mark.asyncio
async def test_no_error():
    result = await StubExecutor().execute(_make_task())
    assert result.error is None


# ── SurplusLLMExecutor tests ──────────────────────────────────────────


def _make_router(*, success=True, content="analysis result", model_id="test-model",
                 provider_used="test-provider", error=None):
    """Build an AsyncMock router returning a configurable route_call result."""
    router = AsyncMock()
    router.route_call = AsyncMock(return_value=MagicMock(
        success=success, content=content, model_id=model_id,
        provider_used=provider_used, error=error,
    ))
    return router


def _make_llm_executor(router=None, db=None):
    return SurplusLLMExecutor(router or _make_router(), db=db or AsyncMock())


# 1. Happy path — router returns success, result has content and insights
@pytest.mark.asyncio
async def test_llm_execute_happy_path():
    router = _make_router(content="  great analysis  ")
    executor = _make_llm_executor(router=router)

    with patch("genesis.surplus.executor.observations.query", new_callable=AsyncMock, return_value=[]):
        result = await executor.execute(_make_task())

    assert result.success is True
    assert result.content == "great analysis"  # stripped
    assert len(result.insights) == 1
    assert result.error is None


# 2. Router returns success=False
@pytest.mark.asyncio
async def test_llm_execute_router_failure():
    router = _make_router(success=False, error="rate limited")
    executor = _make_llm_executor(router=router)

    with patch("genesis.surplus.executor.observations.query", new_callable=AsyncMock, return_value=[]):
        result = await executor.execute(_make_task())

    assert result.success is False
    assert "rate limited" in result.error


# 3. Router raises RuntimeError
@pytest.mark.asyncio
async def test_llm_execute_router_exception():
    router = AsyncMock()
    router.route_call = AsyncMock(side_effect=RuntimeError("connection lost"))
    executor = _make_llm_executor(router=router)

    with patch("genesis.surplus.executor.observations.query", new_callable=AsyncMock, return_value=[]):
        result = await executor.execute(_make_task())

    assert result.success is False
    assert "RuntimeError" in result.error
    assert "connection lost" in result.error


# 4. Router returns success=True but empty content
@pytest.mark.asyncio
async def test_llm_execute_empty_content():
    router = _make_router(success=True, content="")
    executor = _make_llm_executor(router=router)

    with patch("genesis.surplus.executor.observations.query", new_callable=AsyncMock, return_value=[]):
        result = await executor.execute(_make_task())

    assert result.success is False
    assert "empty" in result.error.lower()


# 5. Call-site mapping — each task type routes to the correct call site
@pytest.mark.asyncio
async def test_llm_call_site_mapping():
    cases = [
        (TaskType.INFRASTRUCTURE_MONITOR, "37_infrastructure_monitor"),
        (TaskType.BRAINSTORM_USER, "12_surplus_brainstorm"),
        (TaskType.MEMORY_AUDIT, "12_surplus_brainstorm"),  # default
    ]
    for task_type, expected_call_site in cases:
        router = _make_router()
        executor = _make_llm_executor(router=router)
        task = _make_task(task_type=task_type)

        with (
            patch("genesis.surplus.executor.observations.query", new_callable=AsyncMock, return_value=[]),
            patch("genesis.db.crud.awareness_ticks.last_tick", new_callable=AsyncMock, return_value=None),
        ):
            await executor.execute(task)

        router.route_call.assert_called_once()
        actual_call_site = router.route_call.call_args[0][0]
        assert actual_call_site == expected_call_site, (
            f"Task {task_type}: expected {expected_call_site}, got {actual_call_site}"
        )


# 6. INFRASTRUCTURE_MONITOR prompt contains "signals" data
@pytest.mark.asyncio
async def test_llm_build_prompt_infra():
    router = _make_router()
    executor = _make_llm_executor(router=router)
    task = _make_task(task_type=TaskType.INFRASTRUCTURE_MONITOR)

    with patch("genesis.db.crud.awareness_ticks.last_tick", new_callable=AsyncMock) as mock_tick:
        mock_tick.return_value = {"signals_json": '[{"name": "cpu", "value": 0.85}]'}
        await executor.execute(task)

    prompt = router.route_call.call_args[0][1][0]["content"]
    assert "cpu" in prompt
    assert "0.85" in prompt


# 7. Analytical (BRAINSTORM_USER) prompt contains observation context
@pytest.mark.asyncio
async def test_llm_build_prompt_analytical():
    router = _make_router()
    executor = _make_llm_executor(router=router)
    task = _make_task(task_type=TaskType.BRAINSTORM_USER)

    obs_data = [{"content": "disk usage growing", "type": "infra", "created_at": "2026-01-15"}]
    with patch("genesis.surplus.executor.observations.query", new_callable=AsyncMock, return_value=obs_data):
        await executor.execute(task)

    prompt = router.route_call.call_args[0][1][0]["content"]
    assert "disk usage growing" in prompt


# 8. Unmapped task type gets generic fallback prompt
@pytest.mark.asyncio
async def test_llm_build_prompt_unmapped_type():
    router = _make_router()
    executor = _make_llm_executor(router=router)
    # CODE_AUDIT is not in _TASK_PROMPTS
    task = _make_task(task_type=TaskType.CODE_AUDIT)

    # _build_prompt should hit the fallback branch (template is None)
    # We need CODE_AUDIT to NOT be in _TASK_PROMPTS; verify that
    from genesis.surplus.executor import _TASK_PROMPTS
    if TaskType.CODE_AUDIT in _TASK_PROMPTS:
        pytest.skip("CODE_AUDIT now has a prompt template; pick another unmapped type")

    with patch("genesis.surplus.executor.observations.query", new_callable=AsyncMock, return_value=[]):
        await executor.execute(task)

    prompt = router.route_call.call_args[0][1][0]["content"]
    assert "code_audit" in prompt
    assert "analysis" in prompt.lower()


# 9. _gather_context for infra: parses signals_json from awareness tick
@pytest.mark.asyncio
async def test_llm_gather_context_infra_signals():
    executor = _make_llm_executor()
    task = _make_task(task_type=TaskType.INFRASTRUCTURE_MONITOR)

    with patch("genesis.db.crud.awareness_ticks.last_tick", new_callable=AsyncMock) as mock_tick:
        mock_tick.return_value = {
            "signals_json": '[{"name": "mem", "value": 0.72}, {"name": "disk", "value": 0.33}]',
        }
        context = await executor._gather_context(task)

    assert "mem" in context
    assert "0.72" in context
    assert "disk" in context
    assert "0.33" in context


# 10. DB error in _gather_context → fallback text, no crash
@pytest.mark.asyncio
async def test_llm_gather_context_db_error():
    executor = _make_llm_executor()

    # Analytical path: observations.query raises
    task_analytical = _make_task(task_type=TaskType.BRAINSTORM_USER)
    with patch("genesis.surplus.executor.observations.query", new_callable=AsyncMock, side_effect=Exception("db locked")):
        context = await executor._gather_context(task_analytical)
    # Should not crash; returns fallback
    assert isinstance(context, str)

    # Infra path: awareness_ticks.last_tick raises
    task_infra = _make_task(task_type=TaskType.INFRASTRUCTURE_MONITOR)
    with patch("genesis.db.crud.awareness_ticks.last_tick", new_callable=AsyncMock, side_effect=Exception("db locked")):
        context = await executor._gather_context(task_infra)
    assert isinstance(context, str)
    assert "unavailable" in context.lower() or "no recent" in context.lower()


# 11. Posts to Telegram via topic_manager with HTML-escaped content
@pytest.mark.asyncio
async def test_llm_post_to_telegram():
    router = _make_router(content="test <b>output</b> & result")
    executor = _make_llm_executor(router=router)
    topic_manager = AsyncMock()
    topic_manager.send_to_category = AsyncMock()
    executor.set_topic_manager(topic_manager)
    task = _make_task(task_type=TaskType.BRAINSTORM_USER)

    with patch("genesis.surplus.executor.observations.query", new_callable=AsyncMock, return_value=[]):
        await executor.execute(task)

    topic_manager.send_to_category.assert_called_once()
    call_args = topic_manager.send_to_category.call_args
    assert call_args[0][0] == "surplus"
    sent_text = call_args[0][1]
    # HTML-escaped: <b> in content should become &lt;b&gt;
    assert "&lt;b&gt;" in sent_text
    assert "&amp;" in sent_text


# 12. Exception in Telegram posting is swallowed
@pytest.mark.asyncio
async def test_llm_post_to_telegram_error_swallowed():
    router = _make_router(content="good result")
    executor = _make_llm_executor(router=router)
    topic_manager = AsyncMock()
    topic_manager.send_to_category = AsyncMock(side_effect=RuntimeError("telegram down"))
    executor.set_topic_manager(topic_manager)
    task = _make_task(task_type=TaskType.BRAINSTORM_USER)

    with patch("genesis.surplus.executor.observations.query", new_callable=AsyncMock, return_value=[]):
        result = await executor.execute(task)

    # Should still succeed despite Telegram failure
    assert result.success is True
    assert result.content == "good result"


# 13. Insight dict structure
@pytest.mark.asyncio
async def test_llm_insight_structure():
    router = _make_router(content="deep insight", model_id="qwen-72b", provider_used="deepinfra")
    executor = _make_llm_executor(router=router)
    task = _make_task(task_type=TaskType.BRAINSTORM_SELF, drive_alignment="growth")

    with patch("genesis.surplus.executor.observations.query", new_callable=AsyncMock, return_value=[]):
        result = await executor.execute(task)

    assert len(result.insights) == 1
    insight = result.insights[0]
    assert insight["content"] == "deep insight"
    assert insight["source_task_type"] == TaskType.BRAINSTORM_SELF
    assert insight["generating_model"] == "qwen-72b"
    assert insight["drive_alignment"] == "growth"
    assert insight["confidence"] == 0.5
