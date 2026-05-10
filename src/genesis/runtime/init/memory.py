"""Init function: _init_memory."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from genesis.util.tasks import tracked_task

_STATUS_WRITE_INTERVAL_S = 60

if TYPE_CHECKING:
    import aiosqlite
    from qdrant_client import QdrantClient

    from genesis.runtime._core import GenesisRuntime

logger = logging.getLogger("genesis.runtime")


async def run_status_writer_loop(
    runtime: GenesisRuntime, interval_s: float = _STATUS_WRITE_INTERVAL_S,
) -> None:
    """Background loop that refreshes status.json on a fixed cadence.

    Decoupled from the awareness tick so a slow tick (e.g. a long Light
    reflection) cannot delay the status.json refresh and trip the watchdog
    into a false restart. Reads ``runtime._status_writer`` on every iteration
    so a future hot-swap picks up the new writer.
    """
    while True:
        try:
            await asyncio.sleep(interval_s)
            writer = runtime._status_writer
            if writer is None:
                continue
            await writer.write()
            runtime.record_job_success("status_writer_loop")
        except asyncio.CancelledError:
            break
        except Exception as exc:
            runtime.record_job_failure("status_writer_loop", str(exc))
            logger.error(
                "Status writer loop iteration failed", exc_info=True,
            )


async def init(rt: GenesisRuntime) -> None:
    """Initialize MemoryStore, HybridRetriever, ContextInjector, and Memory MCP."""
    try:
        from qdrant_client import QdrantClient

        from genesis.env import qdrant_url
        from genesis.memory.embeddings import EmbeddingProvider
        from genesis.memory.linker import MemoryLinker
        from genesis.memory.store import MemoryStore

        qdrant = QdrantClient(url=qdrant_url(), timeout=5)
        from genesis.qdrant.collections import ensure_collections
        ensure_collections(qdrant)
        logger.info("Qdrant collections ensured")

        # One-time migration: move reference vectors from knowledge_base
        # to episodic_memory. References are personal data (credentials,
        # URLs, IPs) that belong alongside episodic memories.
        await _migrate_reference_vectors(qdrant, rt._db)

        # Split embedding chains: storage (Ollama first) vs recall (cloud first).
        # Both share the same L2 diskcache — cache keys are text-based, not
        # provider-dependent, so a write cached via Ollama is instantly
        # available for a read via cloud.
        storage_backends = EmbeddingProvider.build_chain(ollama_first=True)
        recall_backends = EmbeddingProvider.build_chain(ollama_first=False)
        logger.info(
            "Embedding chains: storage=%s, recall=%s",
            [b.name for b in storage_backends],
            [b.name for b in recall_backends],
        )
        storage_embedder = EmbeddingProvider(
            backends=storage_backends,
            activity_tracker=rt._activity_tracker,
            event_bus=rt._event_bus,
        )
        recall_embedder = EmbeddingProvider(
            backends=recall_backends,
            activity_tracker=rt._activity_tracker,
            event_bus=rt._event_bus,
        )

        linker = MemoryLinker(qdrant_client=qdrant, db=rt._db)
        rt._memory_store = MemoryStore(
            embedding_provider=storage_embedder,
            qdrant_client=qdrant,
            db=rt._db,
            linker=linker,
        )
        logger.info("Genesis MemoryStore created (storage embedder)")

        from genesis.cc.context_injector import ContextInjector
        from genesis.memory.retrieval import HybridRetriever

        rt._hybrid_retriever = HybridRetriever(
            embedding_provider=recall_embedder,
            qdrant_client=qdrant,
            db=rt._db,
        )
        rt._context_injector = ContextInjector(
            retriever=rt._hybrid_retriever,
        )
        logger.info("Genesis HybridRetriever + ContextInjector created (recall embedder)")

        from genesis.mcp.memory_mcp import init as init_memory_mcp

        init_memory_mcp(
            db=rt._db,
            qdrant_client=qdrant,
            storage_embedding_provider=storage_embedder,
            recall_embedding_provider=recall_embedder,
            activity_tracker=rt._activity_tracker,
        )
        logger.info("Memory MCP initialized (dual embedders)")

        if rt._result_writer is not None:
            rt._result_writer._memory_store = rt._memory_store
            logger.info("MemoryStore injected into ResultWriter")

        if rt._resilience_state_machine is not None:
            from genesis.resilience.status_writer import StatusFileWriter

            rt._status_writer = StatusFileWriter(
                state_machine=rt._resilience_state_machine,
                deferred_queue=rt._deferred_work_queue,
                dead_letter=rt._dead_letter_queue,
                pending_embeddings_db=rt._db,
                runtime=rt,
            )
            logger.info("StatusFileWriter created")

            # Write once immediately so status.json is fresh the moment the
            # server comes up — don't wait a full interval before the watchdog
            # sees a live heartbeat.
            try:
                await rt._status_writer.write()
            except Exception:
                logger.warning(
                    "Initial status writer write failed", exc_info=True,
                )

            rt._status_writer_task = tracked_task(
                run_status_writer_loop(rt), name="status-writer-loop",
            )
            logger.info(
                "Status writer loop started (interval=%ds)",
                _STATUS_WRITE_INTERVAL_S,
            )

            from genesis.resilience.embedding_recovery import EmbeddingRecoveryWorker
            from genesis.resilience.recovery import RecoveryOrchestrator

            embedding_worker = EmbeddingRecoveryWorker(
                db=rt._db,
                embedding_provider=storage_embedder,
                qdrant_client=qdrant,
            )
            rt._recovery_orchestrator = RecoveryOrchestrator(
                db=rt._db,
                state_machine=rt._resilience_state_machine,
                deferred_queue=rt._deferred_work_queue,
                embedding_worker=embedding_worker,
                dead_letter=rt._dead_letter_queue,
                event_bus=rt._event_bus,
            )
            if rt._router is not None:
                rt._recovery_orchestrator.set_dispatch_fn(rt._router.route_call)
            logger.info("RecoveryOrchestrator created")

        # Wire session observer into awareness loop (needs both store + router)
        if (
            rt._awareness_loop is not None
            and rt._memory_store is not None
            and rt._router is not None
        ):
            try:
                from genesis.memory.session_observer import process_pending_observations

                async def _observer_fn():
                    return await process_pending_observations(
                        store=rt._memory_store,
                        router=rt._router,
                    )

                rt._awareness_loop.set_session_observer(_observer_fn)
                logger.info("Session observer wired to awareness loop")
            except Exception:
                logger.warning("Failed to wire session observer", exc_info=True)

    except (ConnectionError, TimeoutError) as exc:
        logger.error(
            "MemoryStore creation failed (Qdrant down?) — "
            "continuing without vector memory",
            exc_info=True,
        )
        from genesis.runtime._degradation import record_init_degradation
        await record_init_degradation(rt._db, rt._event_bus, "memory", "MemoryStore", str(exc), severity="error")
    except Exception as exc:
        logger.error(
            "MemoryStore creation failed — continuing without vector memory",
            exc_info=True,
        )
        from genesis.runtime._degradation import record_init_degradation
        await record_init_degradation(rt._db, rt._event_bus, "memory", "MemoryStore", str(exc), severity="error")


async def _migrate_reference_vectors(
    qdrant: QdrantClient, db: aiosqlite.Connection,
) -> None:
    """One-time migration: move reference vectors from knowledge_base → episodic_memory.

    Looks up reference qdrant_ids from knowledge_units, checks which ones
    still live in knowledge_base, and moves them to episodic_memory.

    Idempotent: skips entries already in episodic_memory. Safe to run on
    fresh installs (no references in knowledge_base → no-op).
    """
    try:
        cursor = await db.execute(
            "SELECT qdrant_id FROM knowledge_units "
            "WHERE project_type = 'reference' AND qdrant_id IS NOT NULL"
        )
        rows = await cursor.fetchall()
        if not rows:
            return

        ref_ids = [r[0] for r in rows]

        # Check which IDs still exist in knowledge_base
        kb_points = qdrant.retrieve(
            collection_name="knowledge_base",
            ids=ref_ids,
            with_payload=True,
            with_vectors=True,
        )
        if not kb_points:
            logger.debug("Reference vector migration: nothing to migrate")
            return

        # Check which are already in episodic_memory (idempotency)
        existing_ep = qdrant.retrieve(
            collection_name="episodic_memory",
            ids=[str(p.id) for p in kb_points],
            with_payload=False,
            with_vectors=False,
        )
        existing_ep_ids = {str(p.id) for p in existing_ep}

        to_migrate = [p for p in kb_points if str(p.id) not in existing_ep_ids]
        if not to_migrate:
            logger.debug("Reference vector migration: all already in episodic_memory")
            return

        # Upsert into episodic_memory with updated payload
        from qdrant_client.models import PointStruct

        points_to_upsert = []
        for p in to_migrate:
            payload = dict(p.payload) if p.payload else {}
            payload["memory_type"] = "episodic"
            points_to_upsert.append(
                PointStruct(id=str(p.id), vector=p.vector, payload=payload)
            )

        qdrant.upsert(
            collection_name="episodic_memory",
            points=points_to_upsert,
        )

        # Delete from knowledge_base
        from qdrant_client.models import PointIdsList

        qdrant.delete(
            collection_name="knowledge_base",
            points_selector=PointIdsList(points=[str(p.id) for p in to_migrate]),
        )

        logger.info(
            "Reference vector migration: moved %d points from "
            "knowledge_base → episodic_memory",
            len(to_migrate),
        )
    except Exception:
        logger.warning(
            "Reference vector migration failed — will retry on next restart",
            exc_info=True,
        )
