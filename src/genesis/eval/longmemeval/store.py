"""Ephemeral Genesis memory store for LongMemEval — zero production contact.

Each question gets one or two throwaway stores built entirely from scratch
(baseline arms share a link-free store; graph arms get a second store built
``with_linker`` — see ``runner.run_question``):
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

from genesis.memory.linker import DEFAULT_SIMILARITY_THRESHOLD

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import aiosqlite

    from genesis.memory.retrieval import HybridRetriever
    from genesis.memory.store import MemoryStore


@dataclass
class EphemeralStore:
    """A throwaway store + retriever pair sharing one in-memory Qdrant + temp DB.

    ``db`` is the ephemeral SQLite connection — exposed so the graph arm can
    run link queries (``memory_links.neighbors_of``) against this store.
    """

    store: MemoryStore
    retriever: HybridRetriever
    workdir: Path
    db: aiosqlite.Connection | None = None


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
    with_linker: bool = False,
    link_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
) -> AsyncIterator[EphemeralStore]:
    """Build a fresh ephemeral store; tear down all temp state on exit.

    ``embedding_provider`` is injectable (a deterministic fake in tests); when
    omitted, Genesis's real cloud-first embedding chain is used. ``reranker``
    (a VoyageReranker) is wired into the retriever so the ``rerank`` arm is
    real; ``None`` makes ``recall(rerank=True)`` a graceful no-op.

    ``with_linker`` wires a real ``MemoryLinker`` into the store so
    ``store(auto_link=True)`` creates ``memory_links`` (the graph arm's store).
    It is opt-in: ``MemoryStore.store()`` defaults ``auto_link=True``, so an
    always-present linker would let any future direct ``store()`` call create
    links silently on baseline stores.
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
    linker = None
    if with_linker:
        from genesis.memory.linker import MemoryLinker

        linker = MemoryLinker(
            qdrant_client=qdrant,
            db=db,
            similarity_threshold=link_threshold,
        )
    store = MemoryStore(
        embedding_provider=embedder,
        qdrant_client=qdrant,
        db=db,
        linker=linker,
    )
    retriever = HybridRetriever(
        embedding_provider=embedder,
        qdrant_client=qdrant,
        db=db,
        reranker=reranker,
    )

    try:
        yield EphemeralStore(store=store, retriever=retriever, workdir=workdir, db=db)
    finally:
        with contextlib.suppress(Exception):
            await db.close()
        with contextlib.suppress(Exception):
            qdrant.close()
        if embedding_provider is None:
            # We constructed this embedder: close its httpx clients + diskcache
            # BEFORE deleting the workdir its cache lives under. Injected
            # embedders belong to the caller (run_longmemeval shares one per
            # run and closes it itself).
            await close_embedder(embedder)
        shutil.rmtree(workdir, ignore_errors=True)


async def close_embedder(embedder: object) -> None:
    """Best-effort close of an embedder's httpx clients + diskcache.

    ``EmbeddingProvider`` exposes no ``close()``; reach in defensively so a
    future internal change degrades to a no-op rather than raising.
    """
    for backend in getattr(embedder, "backends", []) or []:
        http_client = getattr(backend, "_client", None)
        if http_client is not None and hasattr(http_client, "aclose"):
            with contextlib.suppress(Exception):
                await http_client.aclose()
    cache = getattr(embedder, "_disk_cache", None)
    if cache is not None and hasattr(cache, "close"):
        with contextlib.suppress(Exception):
            cache.close()
