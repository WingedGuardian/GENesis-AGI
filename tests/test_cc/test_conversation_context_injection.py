"""Tests for context injection wiring in ConversationLoop."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from genesis.cc.context_injector import ContextInjector
from genesis.cc.conversation import ConversationLoop


def _make_loop(*, context_injector=None):
    db = AsyncMock()
    invoker = MagicMock()
    assembler = MagicMock()
    assembler.assemble = AsyncMock(return_value="base system prompt")
    return ConversationLoop(
        db=db,
        invoker=invoker,
        assembler=assembler,
        context_injector=context_injector,
    )


class TestContextInjectionWiring:
    @pytest.mark.asyncio
    async def test_enrich_appends_context(self):
        injector = ContextInjector()
        injector.inject = AsyncMock(return_value="## Relevant Prior Experience\n- memory1")
        loop = _make_loop(context_injector=injector)

        result = await loop._enrich_with_context("base prompt", "query")
        assert result == "base prompt\n\n## Relevant Prior Experience\n- memory1"

    @pytest.mark.asyncio
    async def test_enrich_no_injector(self):
        loop = _make_loop()
        result = await loop._enrich_with_context("base prompt", "query")
        assert result == "base prompt"

    @pytest.mark.asyncio
    async def test_enrich_empty_context(self):
        injector = ContextInjector()
        injector.inject = AsyncMock(return_value="")
        loop = _make_loop(context_injector=injector)

        result = await loop._enrich_with_context("base prompt", "query")
        assert result == "base prompt"

    @pytest.mark.asyncio
    async def test_enrich_timeout_returns_original(self):
        injector = ContextInjector()
        injector.inject = AsyncMock(side_effect=TimeoutError)
        loop = _make_loop(context_injector=injector)

        result = await loop._enrich_with_context("base prompt", "query")
        assert result == "base prompt"

    @pytest.mark.asyncio
    async def test_enrich_none_prompt_passthrough(self):
        injector = ContextInjector()
        loop = _make_loop(context_injector=injector)

        result = await loop._enrich_with_context(None, "query")
        assert result is None
