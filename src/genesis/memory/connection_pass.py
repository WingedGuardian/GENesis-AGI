"""Cross-session connection discovery for newly extracted memories.

After the extraction pipeline stores new memories (FTS5-only), this pass
embeds each one and searches Qdrant for similar memories from OTHER sessions.
Genuine cross-session connections are stored as ``related_to`` links in
``memory_links``.

Purely vector-based — no LLM call.  Cost: $0.00 per cycle (local embeddings
+ Qdrant search).  Latency: ~2-4s for a typical 15-30 memory cycle.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import aiosqlite
    from qdrant_client import QdrantClient

    from genesis.memory.embeddings import EmbeddingProvider

logger = logging.getLogger(__name__)

# Tuning knobs
_SIMILARITY_THRESHOLD = 0.80  # Higher than auto_link (0.75) to reduce noise
_MAX_LINKS_PER_MEMORY = 3
_MAX_TOTAL_LINKS = 50
_SEARCH_LIMIT = 10  # Qdrant candidates per query


async def run_connection_pass(
    *,
    db: aiosqlite.Connection,
    qdrant_client: QdrantClient,
    embedding_provider: EmbeddingProvider,
    newly_stored: list[tuple[str, str, str]],
    similarity_threshold: float = _SIMILARITY_THRESHOLD,
    max_links_per_memory: int = _MAX_LINKS_PER_MEMORY,
    max_total_links: int = _MAX_TOTAL_LINKS,
) -> dict:
    """Discover cross-session connections for newly extracted memories.

    Parameters
    ----------
    newly_stored
        List of ``(memory_id, content, source_session_id)`` tuples from
        the extraction cycle.

    Returns
    -------
    dict with ``connections_created``, ``memories_scanned``, ``errors``.
    """
    from genesis.memory.embeddings import EmbeddingUnavailableError

    result = {"connections_created": 0, "memories_scanned": 0, "errors": 0}
    if not newly_stored:
        return result

    total_links = 0
    now_iso = datetime.now(UTC).isoformat()

    for memory_id, content, source_session_id in newly_stored:
        if total_links >= max_total_links:
            break
        result["memories_scanned"] += 1

        try:
            # Embed with the same enrichment pattern as MemoryStore
            from genesis.memory.embeddings import EmbeddingProvider as _EP

            enriched = _EP.enrich(content, "episodic", [])
            vector = await embedding_provider.embed(enriched)
        except EmbeddingUnavailableError:
            logger.debug(
                "Embedding unavailable for connection pass, skipping memory %s",
                memory_id[:8],
            )
            result["errors"] += 1
            continue
        except Exception:
            logger.warning(
                "Unexpected embedding error for %s", memory_id[:8], exc_info=True,
            )
            result["errors"] += 1
            continue

        try:
            neighbors = _find_cross_session_neighbors(
                qdrant_client=qdrant_client,
                query_vector=vector,
                source_session_id=source_session_id,
                similarity_threshold=similarity_threshold,
            )
        except Exception:
            logger.warning(
                "Qdrant search failed for %s", memory_id[:8], exc_info=True,
            )
            result["errors"] += 1
            continue

        links_this_memory = 0
        for neighbor in neighbors:
            if links_this_memory >= max_links_per_memory:
                break
            if total_links >= max_total_links:
                break

            target_id = neighbor["id"]
            score = neighbor["score"]

            try:
                from genesis.db.crud import memory_links

                await memory_links.create(
                    db,
                    source_id=memory_id,
                    target_id=target_id,
                    link_type="related_to",
                    strength=round(score, 4),
                    created_at=now_iso,
                )
                links_this_memory += 1
                total_links += 1
                result["connections_created"] += 1
            except sqlite3.IntegrityError:
                # Duplicate PK (source_id, target_id) — expected, skip
                pass
            except Exception:
                logger.debug(
                    "Failed to create link %s→%s",
                    memory_id[:8], target_id[:8], exc_info=True,
                )

    # Invalidate graph cache once if we created any links
    if result["connections_created"] > 0:
        try:
            from genesis.memory.graph import invalidate_graph_cache

            invalidate_graph_cache()
        except Exception:
            pass

    if result["connections_created"] > 0:
        logger.info(
            "Connection pass: %d links created across %d memories (%d errors)",
            result["connections_created"],
            result["memories_scanned"],
            result["errors"],
        )

    return result


def _find_cross_session_neighbors(
    *,
    qdrant_client: QdrantClient,
    query_vector: list[float],
    source_session_id: str,
    similarity_threshold: float,
) -> list[dict]:
    """Search Qdrant for similar memories from different sessions.

    Uses ``must_not`` filter on ``source_session_id`` to exclude same-session
    results, and on ``deprecated`` to skip dream-cycle soft-deletes.
    """
    from qdrant_client.models import FieldCondition, Filter, MatchValue

    must_not = [
        FieldCondition(key="deprecated", match=MatchValue(value=True)),
    ]
    if source_session_id:
        must_not.append(
            FieldCondition(
                key="source_session_id",
                match=MatchValue(value=source_session_id),
            )
        )

    query_filter = Filter(must_not=must_not)

    results = qdrant_client.query_points(
        collection_name="episodic_memory",
        query=query_vector,
        limit=_SEARCH_LIMIT,
        query_filter=query_filter,
    )

    return [
        {"id": str(hit.id), "score": hit.score, "payload": hit.payload}
        for hit in results.points
        if hit.score >= similarity_threshold
    ]
