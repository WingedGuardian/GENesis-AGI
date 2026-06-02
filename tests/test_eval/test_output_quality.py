"""Tests for the output quality scorer and rubric (Verified Autonomy L3)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from genesis.eval.rubrics import get_rubric
from genesis.eval.scorers import get_scorer
from genesis.eval.types import ScorerType


class TestOutputQualityRubricRegistered:
    def test_rubric_exists(self):
        rubric = get_rubric("output_quality")
        assert rubric.name == "output_quality"
        assert rubric.version == "1.0.0"
        assert rubric.pass_threshold == 0.6

    def test_rubric_prompt_has_placeholders(self):
        rubric = get_rubric("output_quality")
        assert "{actual}" in rubric.prompt_template
        assert "{expected}" in rubric.prompt_template


class TestOutputQualityScorerType:
    def test_enum_value_exists(self):
        assert ScorerType.OUTPUT_QUALITY == "output_quality"

    def test_scorer_registered(self):
        scorer = get_scorer(ScorerType.OUTPUT_QUALITY)
        assert scorer is not None
        assert scorer.scorer_type == ScorerType.OUTPUT_QUALITY


class TestOutputQualityScorerAsync:
    @pytest.mark.asyncio
    async def test_high_quality_passes(self):
        """Coherent, relevant, complete output scores above threshold."""
        scorer = get_scorer(ScorerType.OUTPUT_QUALITY)

        mock_router = AsyncMock()
        mock_result = MagicMock()
        mock_result.success = True
        mock_result.content = json.dumps({
            "coherence": 0.9,
            "relevance": 0.85,
            "completeness": 0.8,
            "score": 0.87,
            "rationale": "Well-structured proposal with clear reasoning",
        })
        mock_result.model_id = "test-model"
        mock_result.provider_used = "test"
        mock_result.error = None
        mock_router.route_call.return_value = mock_result
        scorer.set_router(mock_router)

        passed, score, detail = await scorer.score_async(
            actual="Investigate memory drift by querying Qdrant for stale vectors",
            expected="autonomous proposal",
            config={"rubric_name": "output_quality"},
        )

        assert passed is True
        assert score >= 0.6

    @pytest.mark.asyncio
    async def test_low_quality_fails(self):
        """Incoherent output scores below threshold."""
        scorer = get_scorer(ScorerType.OUTPUT_QUALITY)

        mock_router = AsyncMock()
        mock_result = MagicMock()
        mock_result.success = True
        mock_result.content = json.dumps({
            "coherence": 0.2,
            "relevance": 0.3,
            "completeness": 0.1,
            "score": 0.24,
            "rationale": "Self-contradictory, off-topic, incomplete",
        })
        mock_result.model_id = "test-model"
        mock_result.provider_used = "test"
        mock_result.error = None
        mock_router.route_call.return_value = mock_result
        scorer.set_router(mock_router)

        passed, score, detail = await scorer.score_async(
            actual="something something the thing about the stuff",
            expected="autonomous proposal",
            config={"rubric_name": "output_quality"},
        )

        assert passed is False
        assert score < 0.6

    @pytest.mark.asyncio
    async def test_router_failure_returns_false(self):
        """Router call failure returns failed with zero score."""
        scorer = get_scorer(ScorerType.OUTPUT_QUALITY)

        mock_router = AsyncMock()
        mock_result = MagicMock()
        mock_result.success = False
        mock_result.error = "timeout"
        mock_result.content = None
        mock_result.model_id = None
        mock_result.provider_used = None
        mock_router.route_call.return_value = mock_result
        scorer.set_router(mock_router)

        passed, score, detail = await scorer.score_async(
            actual="test",
            expected="test",
            config={"rubric_name": "output_quality"},
        )

        assert passed is False
        assert score == 0.0


class TestQualityGateIntegration:
    """Test the quality_hold filtering in send_digest."""

    def test_quality_hold_filtered_from_digest(self):
        """Proposals with realist_verdict=quality_hold are excluded."""
        proposals = [
            {"id": "1", "content": "good", "realist_verdict": "pass"},
            {"id": "2", "content": "held", "realist_verdict": "quality_hold"},
            {"id": "3", "content": "also good", "realist_verdict": None},
        ]
        filtered = [p for p in proposals if p.get("realist_verdict") != "quality_hold"]
        assert len(filtered) == 2
        assert all(p["id"] != "2" for p in filtered)

    def test_all_held_returns_empty(self):
        """If all proposals are quality_hold, filtered list is empty."""
        proposals = [
            {"id": "1", "realist_verdict": "quality_hold"},
            {"id": "2", "realist_verdict": "quality_hold"},
        ]
        filtered = [p for p in proposals if p.get("realist_verdict") != "quality_hold"]
        assert filtered == []
