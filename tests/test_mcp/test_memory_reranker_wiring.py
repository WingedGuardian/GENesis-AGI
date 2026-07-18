"""genesis.mcp.memory.init() must thread the Voyage reranker into the MCP-path
HybridRetriever.

Regression guard for the bug this fixes: the MCP retriever was built with no
reranker, so memory_recall / knowledge_recall (which default rerank=True) never
actually reranked — _maybe_rerank short-circuits on a None reranker. We stub the
four constructors init() builds so the test is fast, network-free, and
install-agnostic; only the reranker threading is under test.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import genesis.mcp.memory as mem


def _stub_init_deps(monkeypatch) -> dict:
    """Replace the constructors init() calls; capture HybridRetriever kwargs."""
    captured: dict = {}

    class _FakeRetriever:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self._reranker = kwargs.get("reranker")

    # Local imports inside init() resolve these attributes at call time, so
    # patching the source module attribute is sufficient.
    monkeypatch.setattr("genesis.memory.retrieval.HybridRetriever", _FakeRetriever)
    monkeypatch.setattr("genesis.memory.store.MemoryStore", lambda **kw: MagicMock(name="store"))
    monkeypatch.setattr("genesis.memory.linker.MemoryLinker", lambda **kw: MagicMock(name="linker"))
    monkeypatch.setattr(
        "genesis.memory.user_model.UserModelEvolver", lambda **kw: MagicMock(name="ume")
    )
    monkeypatch.setattr(
        "genesis.bookmark.manager.BookmarkManager", lambda **kw: MagicMock(name="bm")
    )
    return captured


def test_init_threads_reranker_into_retriever(monkeypatch):
    captured = _stub_init_deps(monkeypatch)
    sentinel = object()

    mem.init(
        db=MagicMock(),
        qdrant_client=MagicMock(),
        embedding_provider=MagicMock(),
        reranker=sentinel,
    )

    assert captured["reranker"] is sentinel
    # The module retriever exposes the same reranker the tools will consult.
    assert mem._retriever._reranker is sentinel


def test_init_without_reranker_is_none(monkeypatch):
    # Legacy call sites pass no reranker — behavior must be unchanged (None),
    # which is exactly what left the MCP path un-reranked before this fix.
    captured = _stub_init_deps(monkeypatch)

    mem.init(
        db=MagicMock(),
        qdrant_client=MagicMock(),
        embedding_provider=MagicMock(),
    )

    assert captured["reranker"] is None
