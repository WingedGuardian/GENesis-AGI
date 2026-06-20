"""WS-7 / D12: the voice handler marks external-world KB snippets.

Recalled knowledge_base content injected into a spoken response must be flagged
external so it isn't voiced as Genesis's own first-party memory.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from genesis.channels.voice.handler import VoiceConversationHandler


def _router():
    router = MagicMock()
    res = MagicMock()
    res.success = True
    res.content = "ok"
    router.route_call = AsyncMock(return_value=res)
    return router


@pytest.mark.asyncio
async def test_voice_labels_external_kb_snippet():
    retriever = AsyncMock()
    kb = MagicMock()
    kb.content = "FastAPI is built on Starlette"
    kb.collection = "knowledge_base"
    retriever.recall.return_value = [kb]

    handler = VoiceConversationHandler(retriever=retriever, router=_router())
    out = await handler.handle("how does fastapi work", "sess-kb", raw_snippets=True)

    assert "[external-world knowledge]" in out
    assert "FastAPI is built on Starlette" in out


@pytest.mark.asyncio
async def test_voice_episodic_snippet_unlabeled():
    retriever = AsyncMock()
    ep = MagicMock()
    ep.content = "we discussed the roadmap"
    ep.collection = "episodic_memory"
    retriever.recall.return_value = [ep]

    handler = VoiceConversationHandler(retriever=retriever, router=_router())
    out = await handler.handle("what did we discuss", "sess-ep", raw_snippets=True)

    assert "[external-world knowledge]" not in out
    assert "we discussed the roadmap" in out
