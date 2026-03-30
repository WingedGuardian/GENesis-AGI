"""Tests for user question parsing and routing through OutputRouter."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from genesis.reflection.output_router import OutputRouter, parse_deep_reflection_output
from genesis.reflection.types import DeepReflectionOutput, UserQuestion

# ── Parser tests ─────────────────────────────────────────────────────


class TestParserUserQuestion:
    def test_parser_extracts_user_question(self):
        """Parser gets question from JSON."""
        raw = json.dumps({
            "observations": ["obs1"],
            "confidence": 0.8,
            "user_question": {
                "text": "Should we focus on memory or outreach?",
                "context": "Memory backlog growing",
                "options": ["Memory", "Outreach", "Both"],
            },
        })
        output = parse_deep_reflection_output(raw)
        assert output.user_question is not None
        assert output.user_question.text == "Should we focus on memory or outreach?"
        assert output.user_question.context == "Memory backlog growing"
        assert output.user_question.options == ["Memory", "Outreach", "Both"]

    def test_parser_defaults_none(self):
        """No user_question → None."""
        raw = json.dumps({
            "observations": ["obs1"],
            "confidence": 0.8,
        })
        output = parse_deep_reflection_output(raw)
        assert output.user_question is None

    def test_parser_ignores_empty_text(self):
        """user_question with empty text → None."""
        raw = json.dumps({
            "observations": ["obs1"],
            "confidence": 0.8,
            "user_question": {"text": "", "context": "some context"},
        })
        output = parse_deep_reflection_output(raw)
        assert output.user_question is None

    def test_parser_ignores_non_dict(self):
        """user_question as string → None."""
        raw = json.dumps({
            "observations": ["obs1"],
            "confidence": 0.8,
            "user_question": "just a string",
        })
        output = parse_deep_reflection_output(raw)
        assert output.user_question is None

    def test_parser_handles_no_options(self):
        """user_question without options → empty list."""
        raw = json.dumps({
            "observations": ["obs1"],
            "confidence": 0.8,
            "user_question": {
                "text": "What should we prioritize?",
                "context": "Multiple things competing",
            },
        })
        output = parse_deep_reflection_output(raw)
        assert output.user_question is not None
        assert output.user_question.options == []


# ── Router tests ─────────────────────────────────────────────────────


class TestRouteUserQuestion:
    @pytest.mark.asyncio
    async def test_route_surfaces_question(self):
        """question + gate + pipeline → submit called."""
        mock_gate = AsyncMock()
        mock_gate.can_ask.return_value = True
        mock_gate.record_question.return_value = "obs-123"

        mock_pipeline = AsyncMock()

        router = OutputRouter(
            question_gate=mock_gate,
            outreach_pipeline=mock_pipeline,
        )
        output = DeepReflectionOutput(
            observations=["obs1"],
            confidence=0.8,
            user_question=UserQuestion(
                text="Should we refactor the router?",
                context="Complexity is growing",
                options=["Yes, full refactor", "No, defer"],
            ),
        )

        db = AsyncMock()
        summary = await router.route(output, db)

        assert summary["question_surfaced"] is True
        mock_gate.can_ask.assert_called_once_with(db)
        mock_gate.record_question.assert_called_once()
        mock_pipeline.submit.assert_called_once()

        # Verify the outreach request details
        request = mock_pipeline.submit.call_args[0][0]
        assert request.signal_type == "reflection_question"
        assert request.source_id == "obs-123"
        assert request.salience_score == 0.8
        assert "Options:" in request.context

    @pytest.mark.asyncio
    async def test_route_skips_when_pending(self):
        """Pending question → submit not called."""
        mock_gate = AsyncMock()
        mock_gate.can_ask.return_value = False

        mock_pipeline = AsyncMock()

        router = OutputRouter(
            question_gate=mock_gate,
            outreach_pipeline=mock_pipeline,
        )
        output = DeepReflectionOutput(
            observations=["obs1"],
            confidence=0.8,
            user_question=UserQuestion(
                text="Should we refactor?",
                context="complexity",
            ),
        )

        db = AsyncMock()
        summary = await router.route(output, db)

        assert summary["question_surfaced"] is False
        mock_gate.can_ask.assert_called_once()
        mock_gate.record_question.assert_not_called()
        mock_pipeline.submit.assert_not_called()

    @pytest.mark.asyncio
    async def test_route_handles_no_pipeline(self):
        """None pipeline → no crash, question not surfaced."""
        mock_gate = AsyncMock()

        router = OutputRouter(
            question_gate=mock_gate,
            outreach_pipeline=None,
        )
        output = DeepReflectionOutput(
            observations=["obs1"],
            confidence=0.8,
            user_question=UserQuestion(
                text="Should we refactor?",
                context="complexity",
            ),
        )

        db = AsyncMock()
        summary = await router.route(output, db)

        assert summary["question_surfaced"] is False
        # Gate should not even be checked when pipeline is missing
        mock_gate.can_ask.assert_not_called()

    @pytest.mark.asyncio
    async def test_route_handles_no_gate(self):
        """None gate → no crash, question not surfaced."""
        mock_pipeline = AsyncMock()

        router = OutputRouter(
            question_gate=None,
            outreach_pipeline=mock_pipeline,
        )
        output = DeepReflectionOutput(
            observations=["obs1"],
            confidence=0.8,
            user_question=UserQuestion(
                text="Should we refactor?",
                context="complexity",
            ),
        )

        db = AsyncMock()
        summary = await router.route(output, db)

        assert summary["question_surfaced"] is False
        mock_pipeline.submit.assert_not_called()

    @pytest.mark.asyncio
    async def test_route_handles_no_question(self):
        """No user_question → question_surfaced stays False."""
        mock_gate = AsyncMock()
        mock_pipeline = AsyncMock()

        router = OutputRouter(
            question_gate=mock_gate,
            outreach_pipeline=mock_pipeline,
        )
        output = DeepReflectionOutput(
            observations=["obs1"],
            confidence=0.8,
        )

        db = AsyncMock()
        summary = await router.route(output, db)

        assert summary["question_surfaced"] is False
        mock_gate.can_ask.assert_not_called()
