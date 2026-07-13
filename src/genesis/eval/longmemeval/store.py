"""Ephemeral Genesis memory store for LongMemEval — zero production contact.

Each question gets a throwaway store built entirely from scratch:
  * an in-process ``QdrantClient(":memory:")`` (never the production Qdrant),
  * a fresh temp SQLite created by ``init_db`` under ``~/tmp`` (never the
    production ``genesis.db``),
  * Genesis's real embedding chain (cloud-first).

Because both stores are created empty and destroyed on exit, there is no
snapshot and no prod-delta probe to run (unlike the A3 bench, which reads a
production snapshot). Isolation is guaranteed by construction.
"""

from __future__ import annotations

import contextlib
import shutil
import tempfile
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from genesis.memory.retrieval import HybridRetriever
    from genesis.memory.store import MemoryStore


@dataclass
class EphemeralStore:
    """A throwaway store + retriever pair sharing one in-memory Qdrant + temp DB."""

    store: MemoryStore
    retriever: HybridRetriever
    workdir: Path


def _default_tmp_root() -> Path:
    # Large/transient temp goes to ~/tmp, never /tmp or cc-tmp (project rule).
    root = Path.home() / "tmp" / "longmemeval_runs"
    root.mkdir(parents=True, exist_ok=True)
    return root


@asynccontextmanager
async def ephemeral_store(
    *,
    embedding_provider: object | None = None,
    reranker: object | None = None,
    tmp_root: str | Path | None = None,
) -> AsyncIterator[EphemeralStore]:
    """Build a fresh ephemeral store; tear down all temp state on exit.

    ``embedding_provider`` is injectable (a deterministic fake in tests); when
    omitted, Genesis's real cloud-first embedding chain is used. ``reranker``
    (a VoyageReranker) is wired into the retriever so the ``rerank`` arm is
    real; ``None`` makes ``recall(rerank=True)`` a graceful no-op.
    """
    from qdrant_client import QdrantClient

    from genesis.db.connection import init_db
    from genesis.memory.embeddings import EmbeddingProvider
    from genesis.memory.retrieval import HybridRetriever
    from genesis.memory.store import MemoryStore
    from genesis.qdrant.collections import ensure_collections

    root = Path(tmp_root) if tmp_root else _default_tmp_root()
    root.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix="lme_", dir=str(root)))

    qdrant = QdrantClient(":memory:")
    ensure_collections(qdrant)
    db = await init_db(workdir / "lme.db")

    embedder = embedding_provider or EmbeddingProvider(
        backends=EmbeddingProvider.build_chain(ollama_first=False),
        cache_dir=workdir / "cache",
    )
    store = MemoryStore(embedding_provider=embedder, qdrant_client=qdrant, db=db)
    retriever = HybridRetriever(
        embedding_provider=embedder,
        qdrant_client=qdrant,
        db=db,
        reranker=reranker,
    )

    try:
        yield EphemeralStore(store=store, retriever=retriever, workdir=workdir)
    finally:
        with contextlib.suppress(Exception):
            await db.close()
        with contextlib.suppress(Exception):
            qdrant.close()
        shutil.rmtree(workdir, ignore_errors=True)
