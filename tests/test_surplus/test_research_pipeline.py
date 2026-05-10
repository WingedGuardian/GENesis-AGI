"""Tests for the surplus anticipatory research pipeline components."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from genesis.surplus.brainstorm import _SESSION_TYPE_MAP
from genesis.surplus.executor import _fetch_search_results, _parse_search_queries
from genesis.surplus.pipelines import PIPELINES
from genesis.surplus.types import ComputeTier, TaskType
from genesis.web.types import SearchBackend, SearchResponse, SearchResult

# ── _parse_search_queries ───────────────────────────────────────────


class TestParseSearchQueries:
    def test_parse_numbered_queries(self) -> None:
        raw = "1. query one\n2. query two"
        assert _parse_search_queries(raw) == ["query one", "query two"]

    def test_parse_bulleted_queries(self) -> None:
        raw = "- query one\n* query two"
        assert _parse_search_queries(raw) == ["query one", "query two"]

    def test_parse_max_cap(self) -> None:
        raw = "\n".join(f"{i}. search query number {i}" for i in range(1, 11))
        result = _parse_search_queries(raw, max_queries=3)
        assert len(result) == 3

    def test_parse_filters_short(self) -> None:
        # Lines with 5 or fewer chars after stripping bullets are dropped
        raw = "1. hi\n2. this is a real query"
        result = _parse_search_queries(raw)
        assert result == ["this is a real query"]

    def test_parse_plain_lines(self) -> None:
        raw = "plain query line one\nplain query line two"
        assert _parse_search_queries(raw) == [
            "plain query line one",
            "plain query line two",
        ]

    def test_parse_empty(self) -> None:
        assert _parse_search_queries("") == []


# ── _fetch_search_results ───────────────────────────────────────────


class TestFetchSearchResults:
    @pytest.mark.asyncio
    async def test_fetch_success(self) -> None:
        mock_result = SearchResult(
            title="Test Title",
            url="https://example.com",
            snippet="A short snippet about the topic.",
            backend=SearchBackend.BRAVE,
        )
        mock_response = SearchResponse(
            query="test query",
            results=[mock_result],
            backend_used=SearchBackend.BRAVE,
        )

        mock_searcher = MagicMock()
        mock_searcher.search = AsyncMock(return_value=mock_response)

        with patch("genesis.web._get_searcher", return_value=mock_searcher):
            output = await _fetch_search_results(["test query"])

        assert "test query" in output
        assert "Test Title" in output
        assert f"URL: {mock_result.url}" in output
        assert "A short snippet" in output

    @pytest.mark.asyncio
    async def test_fetch_error(self) -> None:
        mock_response = SearchResponse(
            query="bad query",
            results=[],
            error="Backend timeout",
        )

        mock_searcher = MagicMock()
        mock_searcher.search = AsyncMock(return_value=mock_response)

        with patch("genesis.web._get_searcher", return_value=mock_searcher):
            output = await _fetch_search_results(["bad query"])

        assert "Search failed: Backend timeout" in output

    @pytest.mark.asyncio
    async def test_fetch_no_results(self) -> None:
        mock_response = SearchResponse(
            query="obscure query",
            results=[],
        )

        mock_searcher = MagicMock()
        mock_searcher.search = AsyncMock(return_value=mock_response)

        with patch("genesis.web._get_searcher", return_value=mock_searcher):
            output = await _fetch_search_results(["obscure query"])

        assert "(No results)" in output


# ── Pipeline definition ─────────────────────────────────────────────


class TestPipelineDefinition:
    def test_pipeline_registered(self) -> None:
        assert "anticipatory_research" in PIPELINES

    def test_pipeline_steps(self) -> None:
        pipeline = PIPELINES["anticipatory_research"]
        assert len(pipeline.steps) == 2
        assert pipeline.steps[0].task_type == TaskType.RESEARCH_QUERY_GEN
        assert pipeline.steps[1].task_type == TaskType.ANTICIPATORY_RESEARCH

    def test_pipeline_free_tier(self) -> None:
        pipeline = PIPELINES["anticipatory_research"]
        for step in pipeline.steps:
            assert step.compute_tier == ComputeTier.FREE_API


# ── SELF_UNBLOCK scheduling ─────────────────────────────────────────


class TestSelfUnblockScheduling:
    def test_self_unblock_in_session_map(self) -> None:
        assert TaskType.SELF_UNBLOCK in _SESSION_TYPE_MAP
