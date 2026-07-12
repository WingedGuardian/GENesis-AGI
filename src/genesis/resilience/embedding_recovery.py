"""EmbeddingRecoveryWorker — drains pending embeddings on provider recovery."""

from __future__ import annotations

import asyncio
import logging

import aiosqlite

from genesis.db.crud import memory as memory_crud
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

                # Restore faceting fields so the recovered point survives
                # wing=/room=/life_domain= filtered recall — Qdrant `must`
                # filters exclude a point that LACKS the key, so a payload
                # missing these silently drops out of scoped recall (the
                # normal write path sets all of them, see memory/store.py).
                # wing/room live in memory_metadata (written by create_metadata
                # in the same store() that enqueued this pending row, so the
                # row is present at drain time); life_domain and project_type
                # are not metadata columns — recover them from the
                # `life_domain:`/`project_type:` tags store.py appends (see the
                # tag-mirror there so these faceting keys survive a re-embed).
                taxo = await memory_crud.get_taxonomy(self._db, item["memory_id"])
                life_domain = next(
                    (t.split(":", 1)[1] for t in tag_list
                     if t.startswith("life_domain:")),
                    None,
                )
                project_type = next(
                    (t.split(":", 1)[1] for t in tag_list
                     if t.startswith("project_type:")),
                    None,
                )

                payload = {
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
                if taxo:
                    if taxo.get("wing"):
                        payload["wing"] = taxo["wing"]
                    if taxo.get("room"):
                        payload["room"] = taxo["room"]
                    # WS-3: restore origin_class from the authoritative
                    # metadata row so an outage-recovered point carries the
                    # indexed provenance key B1 gates filter on. Missing
                    # (legacy row) → key omitted; gates fail closed on it.
                    if taxo.get("origin_class"):
                        payload["origin_class"] = taxo["origin_class"]
                if life_domain:
                    payload["life_domain"] = life_domain
                if project_type:
                    payload["project_type"] = project_type
                # Restore provenance fields if queued with them
                for prov_key in (
                    "source_session_id", "transcript_path",
                    "extraction_timestamp", "source_pipeline",
                    "source_subsystem",
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

                # Record the embed in memory_metadata first, then flip the
                # queue row (mark_embedded commits, flushing both). The vector
                # already landed in Qdrant above. Metadata-first is self-healing:
                # if only the metadata write lands, the queue row stays 'pending'
                # and the item is simply re-drained (the upsert is idempotent),
                # while metadata already reflects the real vector — so it is
                # never mislabelled 'failed' by the orphan reconciler. No
                # rollback: self._db is the shared SerializedConnection and a
                # rollback discards EVERY coroutine's pending write (connection.py).
                now = datetime.now(UTC).isoformat()
                try:
                    await self._db.execute(
                        "UPDATE memory_metadata SET embedding_status = 'embedded' "
                        "WHERE memory_id = ?",
                        (item["memory_id"],),
                    )
                    await crud.mark_embedded(self._db, item["id"], embedded_at=now)
                    processed += 1
                except Exception:
                    logger.warning(
                        "Failed to finalize embedding status for %s — will re-drain",
                        item["memory_id"], exc_info=True,
                    )

            except Exception as exc:
                logger.error(
                    "Failed to embed pending item %s: %s", item["id"], exc,
                )
                # Mirror the failure into memory_metadata first, then the queue
                # row (mark_failed commits, flushing both). No rollback — this is
                # the shared SerializedConnection (connection.py). If only the
                # metadata write lands, the item is re-drained and retried; the
                # embed is idempotent and a later success resets metadata to
                # 'embedded'. reset_failed_to_pending flips both back together on
                # retry; reconcile_orphaned_metadata is the longer-term backstop
                # once a 'failed' queue row is purged.
                try:
                    await self._db.execute(
                        "UPDATE memory_metadata SET embedding_status = 'failed' "
                        "WHERE memory_id = ?",
                        (item["memory_id"],),
                    )
                    await crud.mark_failed(
                        self._db, item["id"], error_message=str(exc),
                    )
                except Exception:
                    logger.warning(
                        "Failed to record embedding failure for %s",
                        item["memory_id"], exc_info=True,
                    )

            # Pace between items (skip delay after last item)
            if self._pace_delay > 0 and i < len(items) - 1:
                await asyncio.sleep(self._pace_delay)

        logger.info(
            "Embedding recovery: processed %d/%d items", processed, len(items),
        )
        return processed
