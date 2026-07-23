"""Tests for the focus selector — perceive phase of the unified cognitive loop.

Covers: single signal shortcut, critical preemption, multi-signal LLM call,
router failure fallback, context weight lookup, unknown focus type defaults.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from genesis.ego.focus import (
    _ALWAYS_SECTIONS,
    _DEFAULT_WEIGHTS,
    FOCUS_CONTEXT_WEIGHTS,
    FocusResult,
    FocusSelector,
)
from genesis.ego.signals import EgoSignal

# ── Helpers ────────────────────────────────────────────────────────────────


def _make_signal(
    priority: str = "medium",
    focus_category: str = "proactive",
    summary: str = "test signal",
    focus_id: str | None = None,
    signal_id: str | None = None,
) -> EgoSignal:
    sig = EgoSignal(
        priority=priority,
        focus_category=focus_category,
        summary=summary,
        focus_id=focus_id,
    )
    if signal_id:
        # Override the auto-generated id
        object.__setattr__(sig, "id", signal_id)
    return sig


def _mock_router(response_json: dict | None = None, success: bool = True) -> AsyncMock:
    """Create a mock router that returns a RoutingResult-like object."""
    router = AsyncMock()
    result = MagicMock()
    result.success = success
    result.content = json.dumps(response_json) if response_json else ""
    result.cost_usd = 0.001
    router.route_call = AsyncMock(return_value=result)
    return router


# ── Single signal shortcut (no LLM call) ──────────────────────────────────


@pytest.mark.asyncio
async def test_single_signal_direct_select():
    """Single signal → direct select, no LLM call."""
    router = _mock_router()
    selector = FocusSelector(router)

    sig = _make_signal(focus_category="daily_briefing", focus_id="morning")
    result = await selector.select([sig])

    assert result is not None
    assert result.focus_type == "daily_briefing"
    assert result.focus_id == "morning"
    assert result.perceive_cost_usd == 0.0
    assert "only signal" in result.rationale
    # Router should NOT have been called
    router.route_call.assert_not_called()


# ── Critical signal preemption (no LLM call) ──────────────────────────────


@pytest.mark.asyncio
async def test_critical_signal_preempts():
    """Critical signal → preemption, no LLM call."""
    router = _mock_router()
    selector = FocusSelector(router)

    low = _make_signal(priority="low", summary="minor")
    critical = _make_signal(
        priority="critical",
        focus_category="escalation",
        summary="system down",
    )

    result = await selector.select([critical, low])

    assert result is not None
    assert result.focus_type == "escalation"
    assert "critical signal preemption" in result.rationale
    assert result.perceive_cost_usd == 0.0
    router.route_call.assert_not_called()


# ── Multi-signal LLM selection ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_multi_signal_calls_router():
    """2+ non-critical signals → LLM classification via router."""
    response = {
        "focus_type": "goal_review",
        "focus_id": "goal_abc",
        "rationale": "Goal X stale for 12 days",
        "signals_consumed": ["sig1", "sig2"],
    }
    router = _mock_router(response_json=response)
    selector = FocusSelector(router)

    sig1 = _make_signal(priority="medium", summary="idle tick")
    sig2 = _make_signal(priority="medium", summary="goal stale")

    result = await selector.select([sig1, sig2])

    assert result is not None
    assert result.focus_type == "goal_review"
    assert result.focus_id == "goal_abc"
    assert result.rationale == "Goal X stale for 12 days"
    assert result.perceive_cost_usd == 0.001
    router.route_call.assert_called_once()


# ── Router failure → fallback ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_router_failure_falls_back():
    """Router failure → fallback to highest-priority signal."""
    router = _mock_router(success=False)
    selector = FocusSelector(router)

    high = _make_signal(priority="high", focus_category="reactive", summary="event")
    low = _make_signal(priority="low", summary="idle")

    # Signals are passed pre-sorted (high first from PriorityQueue drain)
    result = await selector.select([high, low])

    assert result is not None
    assert result.focus_type == "reactive"
    assert "fallback" in result.rationale


@pytest.mark.asyncio
async def test_router_parse_failure_falls_back():
    """Unparseable router response → fallback."""
    router = AsyncMock()
    result_obj = MagicMock()
    result_obj.success = True
    result_obj.content = "not valid json at all"
    result_obj.cost_usd = 0.001
    router.route_call = AsyncMock(return_value=result_obj)

    selector = FocusSelector(router)
    sig1 = _make_signal(priority="medium", summary="a")
    sig2 = _make_signal(priority="medium", summary="b")

    result = await selector.select([sig1, sig2])

    assert result is not None
    assert "fallback" in result.rationale


@pytest.mark.asyncio
async def test_router_exception_falls_back():
    """Router exception → fallback."""
    router = AsyncMock()
    router.route_call = AsyncMock(side_effect=RuntimeError("connection failed"))

    selector = FocusSelector(router)
    sig1 = _make_signal(summary="a")
    sig2 = _make_signal(summary="b")

    result = await selector.select([sig1, sig2])
    assert result is not None
    assert "fallback" in result.rationale


# ── Empty signals ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_empty_signals_returns_none():
    router = _mock_router()
    selector = FocusSelector(router)
    result = await selector.select([])
    assert result is None


# ── Context weight lookup ─────────────────────────────────────────────────


def test_known_focus_type_returns_weights():
    weights = FocusSelector.get_context_weights("reactive")
    assert weights == FOCUS_CONTEXT_WEIGHTS["reactive"]
    # Always sections should be "always"
    assert weights["user_model"] == "always"
    assert weights["directives"] == "always"


def test_unknown_focus_type_returns_default():
    weights = FocusSelector.get_context_weights("nonexistent_type")
    assert weights == _DEFAULT_WEIGHTS
    # All non-always sections should be "deep" in default
    assert weights["goals"] == "deep"


def test_always_sections_never_overridden():
    """Verify that always sections are 'always' in every weight config."""
    for focus_type, weights in FOCUS_CONTEXT_WEIGHTS.items():
        for section in _ALWAYS_SECTIONS:
            assert weights[section] == "always", (
                f"{section} should be 'always' in {focus_type}, got {weights[section]}"
            )


def test_reactive_weights_have_deep_escalations():
    """Reactive focus should have deep escalations."""
    weights = FOCUS_CONTEXT_WEIGHTS["reactive"]
    assert weights["escalations"] == "deep"
    # Capabilities gets light awareness for reactive proposals
    assert weights["capabilities"] == "light"


def test_goal_review_weights_have_deep_goals():
    """Goal review focus should have deep goals and progress."""
    weights = FOCUS_CONTEXT_WEIGHTS["goal_review"]
    assert weights["goals"] == "deep"
    assert weights["goal_progress"] == "deep"
    assert weights["execution_outcomes"] == "deep"


def test_capability_improvement_category_has_weights():
    """The advisory capability_improvement focus is fully wired with weights."""
    from genesis.ego.types import FocusCategory

    assert FocusCategory.CAPABILITY_IMPROVEMENT == "capability_improvement"
    assert "capability_improvement" in FOCUS_CONTEXT_WEIGHTS
    weights = FocusSelector.get_context_weights("capability_improvement")
    # Self-model performance sections are deep so the ego can reason on the gap.
    assert weights["capability_performance"] == "deep"
    assert weights["capabilities"] == "deep"
    assert weights["execution_outcomes"] == "deep"
    # Always sections are still always.
    assert weights["user_model"] == "always"


# ── FocusResult dataclass ─────────────────────────────────────────────────


def test_focus_result_defaults():
    fr = FocusResult(focus_type="proactive")
    assert fr.focus_id is None
    assert fr.rationale == ""
    assert fr.signals_consumed == []
    assert fr.context_weights == {}
    assert fr.perceive_cost_usd == 0.0
