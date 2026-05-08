from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

import aiosqlite
from qdrant_client import QdrantClient

from genesis.db.crud import memory as memory_crud
from genesis.db.crud import memory_links as memory_links_crud
from genesis.db.crud import pending_embeddings
from genesis.memory.classification import classify_memory
from genesis.memory.embeddings import EmbeddingProvider, EmbeddingUnavailableError
from genesis.memory.linker import MemoryLinker
from genesis.memory.taxonomy import classify as classify_taxonomy
from genesis.observability.call_site_recorder import record_last_run
from genesis.observability.events import GenesisEventBus
from genesis.observability.provider_activity import track_operation
from genesis.observability.types import Severity, Subsystem
from genesis.qdrant.collections import delete_point, upsert_point

# Qdrant connection errors — broad catch for any transport/protocol failure
try:
    from qdrant_client.http.exceptions import (
        ResponseHandlingException,
        UnexpectedResponse,
    )

    _QDRANT_ERRORS: tuple[type[Exception], ...] = (
        UnexpectedResponse,
        ResponseHandlingException,
        ConnectionError,
        TimeoutError,
        OSError,
    )
except ImportError:  # pragma: no cover — safety for minimal installs
    _QDRANT_ERRORS = (ConnectionError, TimeoutError, OSError)

logger = logging.getLogger(__name__)

_COLLECTION_MAP = {
    "episodic": "episodic_memory",
    "knowledge": "knowledge_base",  # External knowledge → knowledge_base
}


class MemoryStore:
    """Full store pipeline: embed -> Qdrant -> FTS5 -> auto-link."""

    def __init__(
        self,
        *,
        embedding_provider: EmbeddingProvider,
        qdrant_client: QdrantClient,
        db: aiosqlite.Connection,
        linker: MemoryLinker | None = None,
        event_bus: GenesisEventBus | None = None,
    ) -> None:
        self._embeddings = embedding_provider
        self._qdrant = qdrant_client
        self._db = db
        self._linker = linker
        self._event_bus = event_bus

    @property
    def linker(self) -> MemoryLinker | None:
        """Public access to the memory linker for extraction typed links."""
        return self._linker

    async def store(
        self,
        content: str,
        source: str,
        *,
        memory_type: str = "episodic",
        collection: str | None = None,
        tags: list[str] | None = None,
        confidence: float | None = None,
        auto_link: bool = True,
        memory_class: str | None = None,
        source_session_id: str | None = None,
        transcript_path: str | None = None,
        source_line_range: tuple[int, int] | None = None,
        extraction_timestamp: str | None = None,
        source_pipeline: str | None = None,
        wing: str | None = None,
        room: str | None = None,
        force_fts5_only: bool = False,
    ) -> str:
        """Full store pipeline: embed -> Qdrant -> FTS5 -> auto-link. Returns memory_id.

        Args:
            collection: Explicit Qdrant collection override. If provided, bypasses
                ``_COLLECTION_MAP`` lookup. Used by ``knowledge_ingest`` and pipeline
                orchestrator to route domain data to ``knowledge_base`` while the
                default map routes all internal knowledge to ``episodic_memory``.
        """
        # Dedup: skip if exact content already stored (any collection)
        try:
            existing = await memory_crud.find_exact_duplicate(
                self._db, content=content,
            )
            if existing:
                logger.debug("Skipping duplicate memory store: %s", existing)
                return existing
        except Exception:
            # Dedup check is best-effort — never block a store on lookup failure
            logger.warning("Dedup check failed, proceeding with store", exc_info=True)

        # Confidence gate: low-confidence → FTS5 only, skip Qdrant
        # Deferred import to break circular: memory.store ↔ perception
        from genesis.perception.confidence import load_config as load_confidence_config
        from genesis.perception.confidence import should_gate

        # force_fts5_only param skips embedding; confidence gate can also set it
        cfg = load_confidence_config()
        gated, gate_msg = should_gate(confidence, cfg.memory_upsertion)
        if gate_msg:
            logger.info("Memory confidence gate: %s", gate_msg)
        if gated:
            force_fts5_only = True

        memory_id = str(uuid.uuid4())
        now_iso = datetime.now(UTC).isoformat()
        resolved_tags = tags or []
        resolved_collection = collection or _COLLECTION_MAP.get(memory_type, "episodic_memory")
        resolved_class = memory_class or classify_memory(
            content, source=source, source_pipeline=source_pipeline or "",
        )
        # Append class tag for FTS5 discoverability
        class_tag = f"class:{resolved_class}"
        if class_tag not in resolved_tags:
            resolved_tags = [*resolved_tags, class_tag]

        # Taxonomy classification — auto-classify if not explicitly provided
        if not wing or not room:
            taxo = classify_taxonomy(
                content, tags=resolved_tags,
                source=source, source_pipeline=source_pipeline or "",
            )
            wing = wing or taxo.wing
            room = room or taxo.room
        # Append wing tag for FTS5 keyword searchability
        wing_tag = f"wing:{wing}"
        if wing_tag not in resolved_tags:
            resolved_tags = [*resolved_tags, wing_tag]

        embedding_ok = not force_fts5_only
        if embedding_ok:
            try:
                enriched = EmbeddingProvider.enrich(content, memory_type, resolved_tags)
                vector = await self._embeddings.embed(enriched)

                await record_last_run(
                    self._db, "21_embeddings",
                    provider="embedding", model_id="qwen3-embedding",
                    response_text=f"Embedded {len(enriched)} chars → {len(vector)}d vector",
                )

                with track_operation(self._embeddings.tracker, "qdrant.upsert"):
                    payload = {
                        "content": content,
                        "source": source,
                        "memory_type": memory_type,
                        "tags": resolved_tags,
                        "confidence": confidence if confidence is not None else 0.5,
                        "created_at": now_iso,
                        "retrieved_count": 0,
                        "source_type": "memory",
                        "scope": "external" if resolved_collection == "knowledge_base" else "user",
                        "memory_class": resolved_class,
                        "wing": wing,
                        "room": room,
                    }
                    # Provenance fields — trace memory back to source conversation
                    if source_session_id:
                        payload["source_session_id"] = source_session_id
                    if transcript_path:
                        payload["transcript_path"] = transcript_path
                    if source_line_range:
                        payload["source_line_range"] = list(source_line_range)
                    if extraction_timestamp:
                        payload["extraction_timestamp"] = extraction_timestamp
                    if source_pipeline:
                        payload["source_pipeline"] = source_pipeline

                    upsert_point(
                        self._qdrant,
                        collection=resolved_collection,
                        point_id=memory_id,
                        vector=vector,
                        payload=payload,
                    )
            except EmbeddingUnavailableError:
                embedding_ok = False
                logger.warning(
                    "Embedding unavailable for memory %s, falling back to FTS5-only storage",
                    memory_id,
                )
            except _QDRANT_ERRORS:
                embedding_ok = False
                logger.error(
                    "Qdrant connection error storing memory %s — falling back to FTS5-only",
                    memory_id,
                    exc_info=True,
                )
            except Exception:
                embedding_ok = False
                logger.error(
                    "Unexpected error during vector storage for memory %s — falling back to FTS5-only",
                    memory_id,
                    exc_info=True,
                )

        # Always write to FTS5 — include tags for keyword searchability
        await memory_crud.upsert(
            self._db,
            memory_id=memory_id,
            content=content,
            source_type="memory",
            tags=" ".join(resolved_tags) if resolved_tags else "",
            collection=resolved_collection,
        )

        # Write companion metadata (timestamps, confidence, embedding status)
        await memory_crud.create_metadata(
            self._db,
            memory_id=memory_id,
            created_at=now_iso,
            collection=resolved_collection,
            confidence=confidence,
            embedding_status="embedded" if embedding_ok else "pending",
            memory_class=resolved_class,
            wing=wing,
            room=room,
        )

        if not embedding_ok:
            # Queue for later embedding — preserve provenance so the recovery
            # worker can reconstruct the full Qdrant payload.
            await pending_embeddings.create(
                self._db,
                id=str(uuid.uuid4()),
                memory_id=memory_id,
                content=content,
                memory_type=memory_type,
                collection=resolved_collection,
                created_at=now_iso,
                tags=",".join(resolved_tags) if resolved_tags else None,
                source=source,
                confidence=confidence,
                source_session_id=source_session_id,
                transcript_path=transcript_path,
                source_line_range=(
                    f"{source_line_range[0]},{source_line_range[1]}"
                    if source_line_range else None
                ),
                extraction_timestamp=extraction_timestamp,
                source_pipeline=source_pipeline,
            )
            if self._event_bus:
                await self._event_bus.emit(
                    Subsystem.MEMORY,
                    Severity.WARNING,
                    "memory.embedding_skipped",
                    f"Embedding unavailable, memory {memory_id} stored FTS5-only",
                    memory_id=memory_id,
                )
        elif auto_link and self._linker:
            await self._linker.auto_link(memory_id, vector, collection=resolved_collection)

        return memory_id

    async def delete(self, memory_id: str) -> dict:
        """Delete a memory from all layers. Returns per-layer status.

        Tries all layers independently — partial failure is acceptable.
        Qdrant deletes try both collections since the FTS5 collection column
        is unreliable (documented in memory.py:72-73).
        """
        results: dict[str, bool | int] = {}

        # 1. memory_metadata companion table
        results["metadata"] = await memory_crud.delete_metadata(
            self._db, memory_id=memory_id,
        )

        # 2. FTS5 text index
        results["fts5"] = await memory_crud.delete(
            self._db, memory_id=memory_id,
        )

        # 3. Qdrant — try both collections (collection column unreliable)
        for coll in ("episodic_memory", "knowledge_base"):
            try:
                delete_point(self._qdrant, collection=coll, point_id=memory_id)
                results[f"qdrant_{coll}"] = True
            except Exception:
                logger.error(
                    "Qdrant delete failed for %s in %s", memory_id, coll,
                    exc_info=True,
                )
                results[f"qdrant_{coll}"] = False

        # 4. Cascade: memory_links
        results["links_deleted"] = await memory_links_crud.delete_by_memory(
            self._db, memory_id=memory_id,
        )

        # 5. Cascade: pending_embeddings
        results["pending_deleted"] = await pending_embeddings.delete_by_memory(
            self._db, memory_id=memory_id,
        )

        return results
