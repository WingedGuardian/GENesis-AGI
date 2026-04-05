"""EmbeddingRecoveryWorker — drains pending embeddings on provider recovery."""

from __future__ import annotations

import asyncio
import logging

import aiosqlite

from genesis.db.crud import pending_embeddings as crud

logger = logging.getLogger(__name__)


class EmbeddingRecoveryWorker:
    """Drains pending_embeddings at a controlled pace on recovery."""

    def __init__(
        self,
        db: aiosqlite.Connection,
        embedding_provider,
        qdrant_client,
        linker=None,
        pace_per_min: int = 10,
    ) -> None:
        self._db = db
        self._embedder = embedding_provider
        self._qdrant = qdrant_client
        self._linker = linker
        self._pace_delay = 60.0 / pace_per_min if pace_per_min > 0 else 0.0

    async def count_pending(self) -> int:
        """Return count of pending embeddings."""
        return await crud.count_pending(self._db)

    async def drain_pending(self, limit: int | None = None) -> int:
        """Embed and upsert pending items. Returns count of successfully processed items."""
        from datetime import UTC, datetime

        from genesis.memory.classification import classify_memory
        from genesis.qdrant.collections import upsert_point

        fetch_limit = limit if limit is not None else 1000
        items = await crud.query_pending(self._db, limit=fetch_limit)

        if not items:
            return 0

        processed = 0
        for i, item in enumerate(items):
            try:
                # Embed
                vector = await self._embedder.embed(item["content"])

                # Upsert to Qdrant — include full metadata to avoid
                # stripped-payload problem (see reindex_fts_to_qdrant.py fix)
                raw_tags = item.get("tags") or ""
                # pending_embeddings stores tags comma-separated
                tag_list = [t.strip() for t in raw_tags.split(",") if t.strip()] if raw_tags else []
                collection = item.get("collection", "episodic_memory")

                payload = {
                    "memory_id": item["memory_id"],
                    "memory_type": item["memory_type"],
                    "content": item["content"],
                    "source": item.get("source") or "embedding_recovery",
                    "source_type": "memory",
                    "tags": tag_list,
                    "confidence": item.get("confidence") or 0.5,
                    "created_at": item.get("created_at", datetime.now(UTC).isoformat()),
                    "retrieved_count": 0,
                    "scope": "external" if collection == "knowledge_base" else "user",
                }
                # Restore provenance fields if queued with them
                for prov_key in (
                    "source_session_id", "transcript_path",
                    "extraction_timestamp", "source_pipeline",
                ):
                    val = item.get(prov_key)
                    if val:
                        payload[prov_key] = val
                # source_line_range stored as "start,end" — restore as list
                slr = item.get("source_line_range")
                if slr and "," in slr:
                    parts = slr.split(",", 1)
                    payload["source_line_range"] = [int(parts[0]), int(parts[1])]
                # Classify for memory_class (consistent with store.py)
                payload["memory_class"] = classify_memory(
                    item["content"],
                    source=payload.get("source", ""),
                    source_pipeline=payload.get("source_pipeline", ""),
                )

                upsert_point(
                    self._qdrant,
                    collection=item["collection"],
                    point_id=item["memory_id"],
                    vector=vector,
                    payload=payload,
                )

                # Auto-link if linker available
                if self._linker is not None:
                    try:
                        await self._linker.auto_link(
                            item["memory_id"],
                            vector,
                            collection=item["collection"],
                        )
                    except Exception:
                        logger.warning(
                            "Auto-link failed for %s, continuing", item["memory_id"],
                        )

                # Mark completed
                now = datetime.now(UTC).isoformat()
                await crud.mark_embedded(self._db, item["id"], embedded_at=now)
                processed += 1

            except Exception as exc:
                logger.error(
                    "Failed to embed pending item %s: %s", item["id"], exc,
                )
                await crud.mark_failed(
                    self._db, item["id"], error_message=str(exc),
                )

            # Pace between items (skip delay after last item)
            if self._pace_delay > 0 and i < len(items) - 1:
                await asyncio.sleep(self._pace_delay)

        logger.info(
            "Embedding recovery: processed %d/%d items", processed, len(items),
        )
        return processed
