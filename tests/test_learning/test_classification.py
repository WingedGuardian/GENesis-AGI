"""Tests for classification pipeline — outcome + delta + attribution."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import aiosqlite
import pytest

from genesis.db import schema
from genesis.learning.classification.attribution import route_learning_signals
from genesis.learning.classification.delta import DeltaAssessor
from genesis.learning.classification.outcome import OutcomeClassifier
from genesis.learning.types import (
    DeltaClassification,
    DiscoveryAttribution,
    InteractionSummary,
    OutcomeClass,
    RequestDeliveryDelta,
)

# ─── Helpers ─────────────────────────────────────────────────────────────────


@dataclass
class FakeRoutingResult:
    success: bool
    content: str | None


def _make_summary(**overrides: Any) -> InteractionSummary:
    defaults = {
        "session_id": "s1",
        "user_text": "deploy the widget",
        "response_text": "Done, widget deployed.",
        "tool_calls": ["bash"],
        "token_count": 100,
        "channel": "terminal",
        "timestamp": datetime(2026, 3, 9, tzinfo=UTC),
    }
    defaults.update(overrides)
    return InteractionSummary(**defaults)


def _mock_router(content: str, success: bool = True) -> Any:
    result = FakeRoutingResult(success=success, content=content)
    router = AsyncMock()
    router.route_call.return_value = result
    return router


@pytest.fixture
async def db():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        for ddl in schema.TABLES.values():
            await conn.execute(ddl)
        await conn.commit()
        yield conn


# ─── OutcomeClassifier ───────────────────────────────────────────────────────


class TestOutcomeClassifier:
    @pytest.mark.asyncio
    async def test_classify_success(self):
        router = _mock_router(json.dumps({"outcome": "success", "rationale": "ok"}))
        c = OutcomeClassifier(router)
        result = await c.classify(_make_summary())
        assert result == OutcomeClass.SUCCESS
        router.route_call.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_classify_approach_failure(self):
        router = _mock_router(json.dumps({"outcome": "approach_failure", "rationale": "bad"}))
        result = await OutcomeClassifier(router).classify(_make_summary())
        assert result == OutcomeClass.APPROACH_FAILURE

    @pytest.mark.asyncio
    async def test_classify_capability_gap(self):
        router = _mock_router(json.dumps({"outcome": "capability_gap"}))
        result = await OutcomeClassifier(router).classify(_make_summary())
        assert result == OutcomeClass.CAPABILITY_GAP

    @pytest.mark.asyncio
    async def test_classify_external_blocker(self):
        router = _mock_router(json.dumps({"outcome": "external_blocker"}))
        result = await OutcomeClassifier(router).classify(_make_summary())
        assert result == OutcomeClass.EXTERNAL_BLOCKER

    @pytest.mark.asyncio
    async def test_classify_workaround_success(self):
        router = _mock_router(json.dumps({"outcome": "workaround_success"}))
        result = await OutcomeClassifier(router).classify(_make_summary())
        assert result == OutcomeClass.WORKAROUND_SUCCESS

    @pytest.mark.asyncio
    async def test_classify_fallback_on_failure(self):
        router = _mock_router("", success=False)
        result = await OutcomeClassifier(router).classify(_make_summary())
        assert result == OutcomeClass.CLASSIFICATION_FAILED

    @pytest.mark.asyncio
    async def test_classify_fallback_on_bad_json(self):
        router = _mock_router("not json at all")
        result = await OutcomeClassifier(router).classify(_make_summary())
        # Parse failure is an error state, not silent SUCCESS — previously this
        # asserted SUCCESS, which masked real classification failures.
        assert result == OutcomeClass.CLASSIFICATION_FAILED

    @pytest.mark.asyncio
    async def test_classify_extracts_json_from_markdown(self):
        router = _mock_router('Here is the result:\n```json\n{"outcome": "approach_failure"}\n```')
        result = await OutcomeClassifier(router).classify(_make_summary())
        assert result == OutcomeClass.APPROACH_FAILURE

    @pytest.mark.asyncio
    async def test_prompt_includes_all_classes(self):
        router = _mock_router(json.dumps({"outcome": "success"}))
        c = OutcomeClassifier(router)
        await c.classify(_make_summary())
        prompt = router.route_call.call_args[0][1][0]["content"]
        for cls in OutcomeClass:
            if cls == OutcomeClass.CLASSIFICATION_FAILED:
                continue  # Internal sentinel, never returned by the LLM
            assert cls.value in prompt

    @pytest.mark.asyncio
    async def test_prompt_includes_trace_context(self):
        router = _mock_router(json.dumps({"outcome": "success"}))
        c = OutcomeClassifier(router)
        await c.classify(_make_summary(), trace_context="retried 3 times")
        prompt = router.route_call.call_args[0][1][0]["content"]
        assert "retried 3 times" in prompt

    @pytest.mark.asyncio
    async def test_hard_gate_overrides_success_when_goals_failed(self):
        """FM1: partial completion classified as success → forced to approach_failure."""
        response = json.dumps({
            "goals_identified": ["fetch URL A", "fetch URL B"],
            "goals_achieved": ["fetch URL A"],
            "goals_failed": ["fetch URL B"],
            "outcome": "success",
            "rationale": "mostly worked",
        })
        router = _mock_router(response)
        result = await OutcomeClassifier(router).classify(_make_summary())
        assert result == OutcomeClass.APPROACH_FAILURE

    @pytest.mark.asyncio
    async def test_hard_gate_allows_success_when_no_goals_failed(self):
        """FM1: all goals achieved → success is preserved."""
        response = json.dumps({
            "goals_identified": ["deploy widget"],
            "goals_achieved": ["deploy widget"],
            "goals_failed": [],
            "outcome": "success",
            "rationale": "all done",
        })
        router = _mock_router(response)
        result = await OutcomeClassifier(router).classify(_make_summary())
        assert result == OutcomeClass.SUCCESS

    @pytest.mark.asyncio
    async def test_hard_gate_ignores_missing_goals_fields(self):
        """FM1: old-format response without goals fields still works."""
        response = json.dumps({"outcome": "success", "rationale": "ok"})
        router = _mock_router(response)
        result = await OutcomeClassifier(router).classify(_make_summary())
        assert result == OutcomeClass.SUCCESS

    @pytest.mark.asyncio
    async def test_prompt_includes_goal_validation_section(self):
        """FM1: the prompt asks for structured goal validation."""
        router = _mock_router(json.dumps({"outcome": "success"}))
        c = OutcomeClassifier(router)
        await c.classify(_make_summary())
        prompt = router.route_call.call_args[0][1][0]["content"]
        assert "Goal Validation" in prompt
        assert "goals_failed" in prompt


# ─── DeltaAssessor ───────────────────────────────────────────────────────────


class TestDeltaAssessor:
    @pytest.mark.asyncio
    async def test_assess_exact_match(self):
        resp = json.dumps({
            "classification": "exact_match",
            "attributions": [],
            "evidence": "matched perfectly",
            "scope_evolution": None,
        })
        router = _mock_router(resp)
        result = await DeltaAssessor(router).assess(_make_summary())
        assert result.classification == DeltaClassification.EXACT_MATCH
        assert result.attributions == []
        assert result.evidence == "matched perfectly"

    @pytest.mark.asyncio
    async def test_assess_with_attributions(self):
        resp = json.dumps({
            "classification": "acceptable_shortfall",
            "attributions": ["user_model_gap", "scope_underspecified"],
            "evidence": "missed preference",
            "scope_evolution": None,
        })
        result = await DeltaAssessor(_mock_router(resp)).assess(_make_summary())
        assert result.classification == DeltaClassification.ACCEPTABLE_SHORTFALL
        assert DiscoveryAttribution.USER_MODEL_GAP in result.attributions
        assert DiscoveryAttribution.SCOPE_UNDERSPECIFIED in result.attributions

    @pytest.mark.asyncio
    async def test_assess_with_scope_evolution(self):
        resp = json.dumps({
            "classification": "over_delivery",
            "attributions": ["user_revised_scope"],
            "evidence": "user expanded scope",
            "scope_evolution": {
                "original_request": "deploy widget",
                "final_delivery": "deploy widget + tests",
                "scope_communicated": True,
            },
        })
        result = await DeltaAssessor(_mock_router(resp)).assess(_make_summary())
        assert result.scope_evolution is not None
        assert result.scope_evolution.scope_communicated is True

    @pytest.mark.asyncio
    async def test_assess_fallback_on_failure(self):
        result = await DeltaAssessor(_mock_router("", success=False)).assess(_make_summary())
        assert result.classification == DeltaClassification.EXACT_MATCH
        assert result.attributions == []

    @pytest.mark.asyncio
    async def test_assess_fallback_on_bad_json(self):
        result = await DeltaAssessor(_mock_router("garbage")).assess(_make_summary())
        assert result.classification == DeltaClassification.EXACT_MATCH

    @pytest.mark.asyncio
    async def test_assess_invalid_attribution_skipped(self):
        resp = json.dumps({
            "classification": "exact_match",
            "attributions": ["user_model_gap", "INVALID_THING"],
            "evidence": "",
        })
        result = await DeltaAssessor(_mock_router(resp)).assess(_make_summary())
        assert len(result.attributions) == 1
        assert result.attributions[0] == DiscoveryAttribution.USER_MODEL_GAP


# ─── Attribution Routing ─────────────────────────────────────────────────────


class TestAttributionRouting:
    @pytest.mark.asyncio
    async def test_external_limitation(self, db):
        writer = AsyncMock()
        writer.write.return_value = "obs-1"
        delta = RequestDeliveryDelta(
            classification=DeltaClassification.ACCEPTABLE_SHORTFALL,
            attributions=[DiscoveryAttribution.EXTERNAL_LIMITATION],
            scope_evolution=None,
            evidence="API was down",
        )
        actions = await route_learning_signals(db, delta, OutcomeClass.EXTERNAL_BLOCKER, writer)
        assert actions["external_limitation"] == "observation_written"
        writer.write.assert_awaited_once()
        call_kwargs = writer.write.call_args.kwargs
        assert call_kwargs["type"] == "external_limitation"
        assert call_kwargs["priority"] == "medium"

    @pytest.mark.asyncio
    async def test_user_model_gap(self, db):
        writer = AsyncMock()
        writer.write.return_value = "obs-2"
        delta = RequestDeliveryDelta(
            classification=DeltaClassification.MISINTERPRETATION,
            attributions=[DiscoveryAttribution.USER_MODEL_GAP],
            scope_evolution=None,
            evidence="user prefers verbose output",
        )
        actions = await route_learning_signals(db, delta, OutcomeClass.SUCCESS, writer)
        assert actions["user_model_gap"] == "observation_written"
        assert writer.write.call_args.kwargs["priority"] == "high"

    @pytest.mark.asyncio
    async def test_genesis_capability_with_gap_outcome(self, db):
        writer = AsyncMock()
        delta = RequestDeliveryDelta(
            classification=DeltaClassification.ACCEPTABLE_SHORTFALL,
            attributions=[DiscoveryAttribution.GENESIS_CAPABILITY],
            scope_evolution=None,
            evidence="cannot parse PDFs",
        )
        actions = await route_learning_signals(db, delta, OutcomeClass.CAPABILITY_GAP, writer)
        assert actions["genesis_capability"] == "capability_gap_recorded"
        # Should have written to capability_gaps table, not observation_writer
        writer.write.assert_not_awaited()
        # Verify in DB
        cursor = await db.execute("SELECT * FROM capability_gaps")
        rows = await cursor.fetchall()
        assert len(rows) == 1
        assert dict(rows[0])["description"] == "cannot parse PDFs"

    @pytest.mark.asyncio
    async def test_genesis_capability_without_gap_outcome(self, db):
        writer = AsyncMock()
        writer.write.return_value = "obs-3"
        delta = RequestDeliveryDelta(
            classification=DeltaClassification.ACCEPTABLE_SHORTFALL,
            attributions=[DiscoveryAttribution.GENESIS_CAPABILITY],
            scope_evolution=None,
            evidence="could be faster",
        )
        actions = await route_learning_signals(db, delta, OutcomeClass.SUCCESS, writer)
        assert actions["genesis_capability"] == "observation_written"
        assert writer.write.call_args.kwargs["type"] == "capability_improvement"

    @pytest.mark.asyncio
    async def test_genesis_interpretation(self, db):
        writer = AsyncMock()
        writer.write.return_value = "obs-4"
        delta = RequestDeliveryDelta(
            classification=DeltaClassification.MISINTERPRETATION,
            attributions=[DiscoveryAttribution.GENESIS_INTERPRETATION],
            scope_evolution=None,
            evidence="wrong file",
        )
        actions = await route_learning_signals(db, delta, OutcomeClass.APPROACH_FAILURE, writer)
        assert actions["genesis_interpretation"] == "observation_written"

    @pytest.mark.asyncio
    async def test_scope_underspecified(self, db):
        writer = AsyncMock()
        writer.write.return_value = "obs-5"
        delta = RequestDeliveryDelta(
            classification=DeltaClassification.ACCEPTABLE_SHORTFALL,
            attributions=[DiscoveryAttribution.SCOPE_UNDERSPECIFIED],
            scope_evolution=None,
            evidence="ambiguous",
        )
        actions = await route_learning_signals(db, delta, OutcomeClass.SUCCESS, writer)
        assert actions["scope_underspecified"] == "observation_written"

    @pytest.mark.asyncio
    async def test_user_revised_scope(self, db):
        writer = AsyncMock()
        delta = RequestDeliveryDelta(
            classification=DeltaClassification.OVER_DELIVERY,
            attributions=[DiscoveryAttribution.USER_REVISED_SCOPE],
            scope_evolution=None,
            evidence="user changed mind",
        )
        actions = await route_learning_signals(db, delta, OutcomeClass.SUCCESS, writer)
        assert actions["user_revised_scope"] == "tracked"
        writer.write.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_multiple_attributions(self, db):
        writer = AsyncMock()
        writer.write.return_value = "obs-x"
        delta = RequestDeliveryDelta(
            classification=DeltaClassification.MISINTERPRETATION,
            attributions=[
                DiscoveryAttribution.USER_MODEL_GAP,
                DiscoveryAttribution.GENESIS_INTERPRETATION,
            ],
            scope_evolution=None,
            evidence="multiple issues",
        )
        actions = await route_learning_signals(db, delta, OutcomeClass.APPROACH_FAILURE, writer)
        assert "user_model_gap" in actions
        assert "genesis_interpretation" in actions
        assert writer.write.await_count == 2
        # speculative_claim also created for non-success outcomes with evidence
        assert actions.get("speculative_claim") == "created"
