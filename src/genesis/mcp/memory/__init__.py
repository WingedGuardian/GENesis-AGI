"""memory-mcp — memory storage, retrieval, and observations.

Phase 5 implementation. Tool signatures match the architecture spec.
"""

from __future__ import annotations

import importlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from fastmcp import FastMCP

from genesis.db.crud import (
    knowledge as knowledge_crud,
)
from genesis.db.crud import (
    memory_links as memory_links_crud,
)
from genesis.db.crud import (
    observations as observations_crud,
)
from genesis.memory.embeddings import EmbeddingProvider
from genesis.qdrant.collections import get_collection_info

from ._plan_bookmark import _process_plan_bookmark_pending
from ._state import (
    _bookmark_mgr,
    _db,
    _qdrant,
    _retriever,
    _store,
    _user_model_evolver,
)

__all__ = [
    "mcp",
    "init",
    "_store",
    "_retriever",
    "_user_model_evolver",
    "_db",
    "_qdrant",
    "_bookmark_mgr",
    "get_collection_info",
    "knowledge",
    "memory_links",
    "observations",
]

if TYPE_CHECKING:
    import aiosqlite
    from qdrant_client import QdrantClient

    from genesis.db.connection import ReadConnectionPool
    from genesis.memory.reranker import VoyageReranker
    from genesis.routing.circuit_breaker import CircuitBreaker
    from genesis.routing.rate_gate import ProviderRateGate

logger = logging.getLogger(__name__)

mcp = FastMCP("genesis-memory")

_PLAN_BOOKMARK_PENDING = Path.home() / ".genesis" / "plan_bookmark_pending.json"


def init(
    *,
    db: aiosqlite.Connection,
    qdrant_client: QdrantClient,
    storage_embedding_provider: EmbeddingProvider | None = None,
    recall_embedding_provider: EmbeddingProvider | None = None,
    activity_tracker: object | None = None,
    reranker: VoyageReranker | None = None,
    read_pool: ReadConnectionPool | None = None,
    rerank_gate: ProviderRateGate | None = None,
    rerank_breaker: CircuitBreaker | None = None,
    # Backward compat — old callers pass ``embedding_provider``
    embedding_provider: EmbeddingProvider | None = None,
) -> None:
    """Initialize memory MCP with live dependencies.

    Accepts split embedding providers: ``storage_embedding_provider`` for
    writes (MemoryStore) and ``recall_embedding_provider`` for reads
    (HybridRetriever).  If only ``embedding_provider`` is given (old
    callers), it is used for both paths.

    ``reranker`` wires the Voyage cross-encoder into the MCP-path retriever so
    ``memory_recall`` / ``knowledge_recall`` actually rerank (they default
    ``rerank=True`` but were previously built with no reranker, so the
    ``_maybe_rerank`` gate never fired). ``None`` preserves the legacy
    no-rerank behavior; a reranker with no ``API_KEY_VOYAGE`` degrades to a
    no-op exactly as the runtime stack does. The MCP recall tools additionally
    honor the ``reranker`` config mode + ``GENESIS_MEMORY_RERANK_OFF`` kill.
    """
    from genesis.bookmark.manager import BookmarkManager
    from genesis.memory.linker import MemoryLinker
    from genesis.memory.retrieval import HybridRetriever
    from genesis.memory.store import MemoryStore
    from genesis.memory.user_model import UserModelEvolver

    # Resolve providers — support both old and new calling conventions
    store_emb = storage_embedding_provider or embedding_provider
    recall_emb = recall_embedding_provider or store_emb
    if store_emb is None:
        msg = "init() requires storage_embedding_provider or embedding_provider"
        raise TypeError(msg)

    global _store, _retriever, _user_model_evolver, _db, _qdrant, _bookmark_mgr  # noqa: PLW0603
    _db = db
    _qdrant = qdrant_client
    linker = MemoryLinker(qdrant_client=qdrant_client, db=db)
    _store = MemoryStore(
        embedding_provider=store_emb,
        qdrant_client=qdrant_client,
        db=db,
        linker=linker,
    )
    # read_pool: the proactive per-prompt path recalls through THIS retriever
    # (memory/proactive.py uses genesis.mcp.memory._retriever), so the ac27b693
    # read-only pool must reach it here — wiring it only into rt._hybrid_retriever
    # would leave the very hot path the pool targets bypassing it. Same shared
    # pool instance (bounded connections); None keeps the pre-pool behavior.
    # rerank_gate/rerank_breaker: same rationale as read_pool — the proactive hot
    # path reranks through THIS retriever, so the shared gate+breaker must reach it
    # here (same instances as rt._hybrid_retriever) or Voyage's RPM would be
    # enforced twice-over (ac27b693, PR-3). None keeps the unguarded behavior.
    _retriever = HybridRetriever(
        embedding_provider=recall_emb,
        qdrant_client=qdrant_client,
        db=db,
        reranker=reranker,
        read_pool=read_pool,
        rerank_gate=rerank_gate,
        rerank_breaker=rerank_breaker,
    )
    _user_model_evolver = UserModelEvolver(db=db)
    _bookmark_mgr = BookmarkManager(
        memory_store=_store,
        hybrid_retriever=_retriever,
        db=db,
    )

    if activity_tracker is not None:
        from genesis.observability.mcp_middleware import InstrumentationMiddleware

        mcp.add_middleware(InstrumentationMiddleware(activity_tracker, "memory", db=db))


def _require_init() -> None:
    if _store is None or _retriever is None or _db is None:
        raise RuntimeError("memory-mcp not initialized — call init() first")

    if _PLAN_BOOKMARK_PENDING.exists():
        from genesis.util.tasks import tracked_task

        tracked_task(
            _process_plan_bookmark_pending(_bookmark_mgr, _resolve_session_id),
            name="plan-bookmark-pending",
        )


def _resolve_session_id(session_id: str) -> str:
    """Resolve a potentially truncated session ID to a full UUID.

    Scans ~/.genesis/sessions/ for a prefix match. Returns the full UUID
    if exactly one match is found, otherwise returns the input unchanged.
    """
    if len(session_id) >= 36:
        return session_id

    sessions_dir = Path.home() / ".genesis" / "sessions"
    if not sessions_dir.exists():
        return session_id

    matches = [
        d.name for d in sessions_dir.iterdir() if d.is_dir() and d.name.startswith(session_id)
    ]
    if len(matches) == 1:
        return matches[0]

    return session_id


# Import tool modules explicitly so CRUD patch-point names stay intact.
_bookmarks_tools = importlib.import_module(".bookmarks", __name__)  # noqa: F401
_conversation_tools = importlib.import_module(".conversation", __name__)  # noqa: F401
_core_tools = importlib.import_module(".core", __name__)  # noqa: F401
_documents_tools = importlib.import_module(".documents", __name__)  # noqa: F401
_knowledge_tools = importlib.import_module(".knowledge", __name__)  # noqa: F401
_observations_tools = importlib.import_module(".observations", __name__)  # noqa: F401
_procedural_tools = importlib.import_module(".procedural", __name__)  # noqa: F401
_locate_tools = importlib.import_module(".locate", __name__)  # noqa: F401


# Backward-compatible patch points expected by tests and callers.
knowledge = knowledge_crud
memory_links = memory_links_crud
observations = observations_crud
