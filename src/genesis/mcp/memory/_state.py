"""Module-level state for memory_mcp package."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import aiosqlite
    from qdrant_client import QdrantClient

    from genesis.bookmark.manager import BookmarkManager
    from genesis.memory.retrieval import HybridRetriever
    from genesis.memory.store import MemoryStore
    from genesis.memory.user_model import UserModelEvolver

_store: MemoryStore | None = None
_retriever: HybridRetriever | None = None
_user_model_evolver: UserModelEvolver | None = None
_db: aiosqlite.Connection | None = None
_qdrant: QdrantClient | None = None
_bookmark_mgr: BookmarkManager | None = None
