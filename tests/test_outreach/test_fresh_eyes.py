"""Tests for fresh-eyes cross-model surplus review."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from genesis.outreach.fresh_eyes import FreshEyesReview


@pytest.fixture
def mock_router():
    return AsyncMock()


@pytest.mark.asyncio
async def test_approved_when_score_high(mock_router):
    mock_router.route_call.return_value = MagicMock(
        success=True,
        content='{"score": 4, "reason": "Relevant and actionable"}',
        model_id="gemini-free",
        error=None,
    )
    review = FreshEyesReview(mock_router, min_score=3.0)
    result = await review.review("Great insight about caching", "Caching optimization")
    assert result.approved is True
    assert result.score == 4.0
    assert result.model_used == "gemini-free"


@pytest.mark.asyncio
async def test_rejected_when_score_low(mock_router):
    mock_router.route_call.return_value = MagicMock(
        success=True,
        content='{"score": 2, "reason": "Too vague to be actionable"}',
        model_id="gemini-free",
        error=None,
    )
    review = FreshEyesReview(mock_router, min_score=3.0)
    result = await review.review("Something about stuff", "Vague topic")
    assert result.approved is False
    assert result.score == 2.0


@pytest.mark.asyncio
async def test_router_failure_defaults_to_reject(mock_router):
    mock_router.route_call.side_effect = Exception("Router down")
    review = FreshEyesReview(mock_router)
    result = await review.review("Content", "Topic")
    assert result.approved is False
    assert "error" in result.reason.lower()
