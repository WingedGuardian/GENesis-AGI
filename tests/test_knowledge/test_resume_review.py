"""Tests for resume review application."""

import json
from unittest.mock import AsyncMock, MagicMock

from genesis.knowledge.applications.resume_review import ResumeReview, ResumeReviewer


async def test_pass1_happy_path():
    """Pass 1 produces structured analysis."""
    mock_router = MagicMock()
    mock_result = MagicMock()
    mock_result.success = True
    mock_result.content = json.dumps({
        "overall_score": 7.5,
        "strengths": ["Clear formatting", "Good action verbs"],
        "top_priorities": ["Add more metrics", "Improve summary"],
        "sections": {},
    })
    mock_router.route_call = AsyncMock(return_value=mock_result)

    reviewer = ResumeReviewer(router=mock_router)
    result = await reviewer.review("John Doe\nSoftware Engineer\n- Built systems")

    assert isinstance(result, ResumeReview)
    assert result.pass1_analysis.get("overall_score") == 7.5
    assert "7.5/10" in result.combined_output


async def test_empty_resume():
    """Empty resume returns error."""
    mock_router = MagicMock()
    reviewer = ResumeReviewer(router=mock_router)
    result = await reviewer.review("")
    assert result.error == "Empty resume text"


async def test_pass1_failure_graceful():
    """LLM failure in Pass 1 returns error dict, doesn't crash."""
    mock_router = MagicMock()
    mock_result = MagicMock()
    mock_result.success = False
    mock_result.content = None
    mock_router.route_call = AsyncMock(return_value=mock_result)

    reviewer = ResumeReviewer(router=mock_router)
    result = await reviewer.review("Some resume content")
    assert "error" in result.pass1_analysis


async def test_combined_output_formatting():
    """Combined output includes both pass results."""
    mock_router = MagicMock()

    pass1_response = json.dumps({
        "overall_score": 8.0,
        "strengths": ["Strong technical skills"],
        "top_priorities": ["Add leadership examples"],
        "sections": {},
    })
    pass2_response = json.dumps({
        "augmented_suggestions": [
            {"suggestion": "Add AWS cert", "grounded_in": "Candidate has AWS SAA cert", "priority": "high"},
        ],
        "missed_opportunities": [
            {"what": "Cloud migration project", "evidence": "Led 3 migrations per KB"},
        ],
        "overall_assessment": "Resume undersells cloud expertise.",
    })

    call_count = 0

    async def mock_route_call(call_site, messages, **kwargs):
        nonlocal call_count
        call_count += 1
        result = MagicMock()
        result.success = True
        result.content = pass1_response if call_count == 1 else pass2_response
        return result

    mock_router.route_call = mock_route_call

    reviewer = ResumeReviewer(router=mock_router)

    # Patch KB query to return empty (no memory module in tests)
    reviewer._query_knowledge_base = AsyncMock(return_value="Some KB context")

    result = await reviewer.review("John Doe\nSoftware Engineer")

    assert "8.0/10" in result.combined_output
    assert "Knowledge-Augmented" in result.combined_output
    assert len(result.knowledge_citations) == 1
    assert "AWS" in result.knowledge_citations[0]["grounded_in"]
