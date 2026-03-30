"""Tests for ContextInjector."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from genesis.cc.context_injector import ContextInjector
from genesis.memory.types import RetrievalResult


def _make_result(content: str, score: float = 0.85, memory_type: str = "observation"):
    return RetrievalResult(
        memory_id="test-id",
        content=content,
        source="vector",
        memory_type=memory_type,
        score=score,
        vector_rank=1,
        fts_rank=None,
        activation_score=0.5,
        payload={},
    )


class TestContextInjector:
    @pytest.mark.asyncio
    async def test_no_retriever_returns_empty(self):
        injector = ContextInjector()
        result = await injector.inject("some task")
        assert result == ""

    @pytest.mark.asyncio
    async def test_with_results(self):
        retriever = AsyncMock()
        retriever.recall.return_value = [
            _make_result("first result", 0.92, "procedure"),
            _make_result("second result", 0.78),
        ]
        injector = ContextInjector(retriever=retriever)
        result = await injector.inject("some task", limit=3)

        assert "## Relevant Prior Experience" in result
        assert "**[procedure]**" in result
        assert "(score: 0.92)" in result
        assert "first result" in result
        assert "second result" in result
        retriever.recall.assert_called_once_with("some task", source="episodic", limit=3)

    @pytest.mark.asyncio
    async def test_empty_results(self):
        retriever = AsyncMock()
        retriever.recall.return_value = []
        injector = ContextInjector(retriever=retriever)
        result = await injector.inject("task")
        assert result == ""

    @pytest.mark.asyncio
    async def test_retriever_exception(self):
        retriever = AsyncMock()
        retriever.recall.side_effect = RuntimeError("db down")
        injector = ContextInjector(retriever=retriever)
        result = await injector.inject("task")
        assert result == ""

    @pytest.mark.asyncio
    async def test_set_retriever_late_binding(self):
        injector = ContextInjector()
        assert await injector.inject("task") == ""

        retriever = AsyncMock()
        retriever.recall.return_value = [_make_result("found")]
        injector.set_retriever(retriever)

        result = await injector.inject("task")
        assert "found" in result
