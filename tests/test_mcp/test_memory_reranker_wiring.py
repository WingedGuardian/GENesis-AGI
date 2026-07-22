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

import pytest

import genesis.mcp.memory as mem

# init() rebinds these module globals; restore them so a populated state can't
# leak into the *_requires_init tests (which assert an uninitialized module).
_INIT_GLOBALS = (
    "_store",
    "_db",
    "_qdrant",
    "_retriever",
    "_user_model_evolver",
    "_bookmark_mgr",
)


@pytest.fixture(autouse=True)
def _restore_memory_globals():
    saved = {name: getattr(mem, name) for name in _INIT_GLOBALS}
    yield
    for name, value in saved.items():
        setattr(mem, name, value)


def _stub_init_deps(monkeypatch) -> dict:
    """Replace the constructors init() calls; capture HybridRetriever kwargs."""
    captured: dict = {}

    class _FakeRetriever:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self._reranker = kwargs.get("reranker")
            self._read_pool = kwargs.get("read_pool")

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


def test_init_threads_read_pool_into_retriever(monkeypatch):
    """The proactive per-prompt path recalls through the MCP retriever
    (memory/proactive.py uses genesis.mcp.memory._retriever), so the ac27b693
    read-only pool MUST reach it here — wiring it only into rt._hybrid_retriever
    would leave the very hot path the pool targets bypassing it (Codex #1189 P1).
    """
    captured = _stub_init_deps(monkeypatch)
    sentinel = object()

    mem.init(
        db=MagicMock(),
        qdrant_client=MagicMock(),
        embedding_provider=MagicMock(),
        read_pool=sentinel,
    )

    assert captured["read_pool"] is sentinel
    assert mem._retriever._read_pool is sentinel


def test_init_without_read_pool_is_none(monkeypatch):
    # Legacy / eval call sites pass no pool — the retriever falls back to the
    # shared connection (pre-pool behavior), byte-identical.
    captured = _stub_init_deps(monkeypatch)

    mem.init(
        db=MagicMock(),
        qdrant_client=MagicMock(),
        embedding_provider=MagicMock(),
    )

    assert captured["read_pool"] is None
