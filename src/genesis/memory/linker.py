from __future__ import annotations

import logging
from datetime import UTC, datetime
from difflib import SequenceMatcher

import aiosqlite
from qdrant_client import QdrantClient

from genesis.db.crud import memory as memory_crud
from genesis.db.crud import memory_links
from genesis.memory.types import LinkRecord
from genesis.qdrant.collections import search

logger = logging.getLogger(__name__)

# Valid typed link types from schema CHECK constraint
_VALID_LINK_TYPES = frozenset({
    "supports", "contradicts", "extends", "elaborates",
    "discussed_in", "evaluated_for", "decided",
    "action_item_for", "categorized_as", "related_to",
    "succeeded_by", "preceded_by",
})


class MemoryLinker:
    """Find similar memories and create bidirectional links."""

    def __init__(self, *, qdrant_client: QdrantClient, db: aiosqlite.Connection) -> None:
        self._qdrant = qdrant_client
        self._db = db

    async def auto_link(
        self,
        memory_id: str,
        vector: list[float],
        *,
        collection: str = "episodic_memory",
        similarity_threshold: float = 0.75,
        max_links: int = 5,
    ) -> list[LinkRecord]:
        """Find similar memories and create links."""
        results = search(
            self._qdrant,
            collection=collection,
            query_vector=vector,
            limit=max_links + 1,
        )

        now = datetime.now(UTC).isoformat()
        links: list[LinkRecord] = []

        for hit in results:
            target_id = hit["id"]
            score = hit["score"]

            if target_id == memory_id:
                continue
            if score < similarity_threshold:
                continue
            if len(links) >= max_links:
                break

            link_type = "extends" if score >= 0.90 else "supports"

            await memory_links.create(
                self._db,
                source_id=memory_id,
                target_id=target_id,
                link_type=link_type,
                strength=score,
                created_at=now,
            )

            links.append(
                LinkRecord(
                    source_id=memory_id,
                    target_id=target_id,
                    link_type=link_type,
                    strength=score,
                    created_at=now,
                )
            )

        if links:
            from genesis.memory.graph import invalidate_graph_cache
            invalidate_graph_cache()
        return links

    async def count_links(self, memory_id: str) -> int:
        """Count links for a memory."""
        return await memory_links.count_links(self._db, memory_id)

    async def create_typed_links(
        self,
        memory_id: str,
        relationships: list[dict],
    ) -> list[LinkRecord]:
        """Create typed links from extraction relationships.

        For each relationship, searches for the target entity via FTS5
        (with difflib fallback for fuzzy matching) and creates a link.

        Args:
            memory_id: The source memory ID.
            relationships: List of dicts with 'from', 'to', 'type' keys.

        Returns:
            List of created LinkRecords.
        """
        if not relationships:
            return []

        now = datetime.now(UTC).isoformat()
        links: list[LinkRecord] = []

        for rel in relationships:
            link_type = rel.get("type", "")
            target_name = rel.get("to", "")

            if not link_type or not target_name:
                continue

            if link_type not in _VALID_LINK_TYPES:
                logger.debug(
                    "Skipping invalid link type %r for memory %s",
                    link_type, memory_id,
                )
                continue

            # Try FTS5 search first
            target_id = await self._find_entity_by_name(target_name)
            if not target_id:
                logger.debug(
                    "No matching memory found for entity %r (link from %s)",
                    target_name, memory_id,
                )
                continue

            if target_id == memory_id:
                continue

            try:
                await memory_links.create(
                    self._db,
                    source_id=memory_id,
                    target_id=target_id,
                    link_type=link_type,
                    strength=0.7,
                    created_at=now,
                )
                links.append(
                    LinkRecord(
                        source_id=memory_id,
                        target_id=target_id,
                        link_type=link_type,
                        strength=0.7,
                        created_at=now,
                    )
                )
            except Exception:
                # Likely duplicate primary key — link already exists
                logger.debug(
                    "Link %s → %s (%s) already exists or failed",
                    memory_id, target_id, link_type,
                    exc_info=True,
                )

        if links:
            from genesis.memory.graph import invalidate_graph_cache
            invalidate_graph_cache()
        return links

    async def _find_entity_by_name(self, entity_name: str) -> str | None:
        """Find a memory ID matching the given entity name.

        Uses FTS5 keyword search first, then falls back to difflib
        SequenceMatcher for fuzzy matching if FTS5 returns no results.
        """
        # FTS5 search
        results = await memory_crud.search(
            self._db,
            query=entity_name,
            limit=5,
        )

        if results:
            # Prefer exact substring match in content
            for r in results:
                if entity_name.lower() in r["content"].lower():
                    return r["memory_id"]
            # Fall back to best FTS5 match
            return results[0]["memory_id"]

        # Difflib fallback: search broader, then fuzzy-match
        # Use individual words from the entity name
        words = entity_name.split()
        for word in words:
            if len(word) < 3:
                continue
            results = await memory_crud.search(
                self._db,
                query=word,
                limit=10,
            )
            if results:
                best_match = None
                best_ratio = 0.0
                for r in results:
                    ratio = SequenceMatcher(
                        None,
                        entity_name.lower(),
                        r["content"][:200].lower(),
                    ).ratio()
                    if ratio > best_ratio:
                        best_ratio = ratio
                        best_match = r["memory_id"]
                if best_ratio >= 0.3 and best_match is not None:
                    return best_match

        return None
