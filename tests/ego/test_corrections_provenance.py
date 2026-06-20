"""WS-7 / D12: the ego's user-corrections recall is pinned to first-party.

Without ``source='episodic'`` the query "user correction ego" classifies as
GENERAL → both collections, so a knowledge_base item tagged 'user_correction'
could surface as if it were a real user correction. Pinning prevents that.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

import genesis.runtime as runtime_mod
from genesis.ego.context import EgoContextBuilder


@pytest.mark.asyncio
async def test_user_corrections_recall_pinned_to_episodic(monkeypatch):
    retriever = MagicMock()
    retriever.recall = AsyncMock(return_value=[])
    rt = MagicMock()
    rt._hybrid_retriever = retriever
    monkeypatch.setattr(
        runtime_mod.GenesisRuntime, "instance", staticmethod(lambda: rt),
    )

    builder = EgoContextBuilder(db=None)
    await builder._user_corrections_section()

    retriever.recall.assert_awaited_once_with(
        "user correction ego", source="episodic", limit=10,
    )
