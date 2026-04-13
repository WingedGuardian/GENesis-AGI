"""Algorithmic health checks on the Genesis memory store.

Pure SQL aggregates and Qdrant vector queries — NO LLM judgment.
"""

from __future__ import annotations

import logging
import random
from datetime import UTC, datetime, timedelta

import aiosqlite

logger = logging.getLogger(__name__)

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


async def near_duplicate_stats(
    db: aiosqlite.Connection,
    qdrant_client,
    *,
    collection: str = "episodic_memory",
    sample_size: int = 100,
    threshold: float = 0.95,
) -> dict:
    """Sample memories and check Qdrant for near-duplicates above *threshold*."""
    try:
        cursor = await db.execute("SELECT memory_id FROM memory_metadata")
        all_ids = [row[0] for row in await cursor.fetchall()]
    except aiosqlite.Error:
        logger.error("Failed to query memory_metadata for duplicate check", exc_info=True)
        return {"error": "DB unavailable"}

    if not all_ids:
        return {"total_sampled": 0, "near_duplicates_found": 0, "pairs": []}

    sampled = random.sample(all_ids, min(sample_size, len(all_ids)))
    pairs: list[tuple[str, str, float]] = []

    try:
        for mem_id in sampled:
            results = qdrant_client.scroll(
                collection_name=collection,
                scroll_filter={"must": [{"key": "memory_id", "match": {"value": mem_id}}]},
                limit=1,
                with_vectors=True,
            )
            points, _ = results
            if not points:
                continue
            vector = points[0].vector
            hits = qdrant_client.search(
                collection_name=collection,
                query_vector=vector,
                limit=2,  # top hit is self
                score_threshold=threshold,
            )
            for hit in hits:
                hit_id = hit.payload.get("memory_id", str(hit.id))
                if hit_id != mem_id and hit.score >= threshold:
                    pairs.append((mem_id, hit_id, round(hit.score, 4)))
    except _QDRANT_ERRORS as exc:
        logger.error("Qdrant unavailable during duplicate check: %s", exc, exc_info=True)
        return {"error": f"Qdrant unavailable: {exc}"}
    except Exception as exc:
        logger.error("Unexpected error during duplicate check: %s", exc, exc_info=True)
        return {"error": f"Unexpected: {exc}"}

    return {
        "total_sampled": len(sampled),
        "near_duplicates_found": len(pairs),
        "pairs": pairs,
    }


async def orphan_stats(db: aiosqlite.Connection, *, min_age_days: int = 7) -> dict:
    """Count memories with no links and older than *min_age_days*."""
    cutoff = (datetime.now(UTC) - timedelta(days=min_age_days)).isoformat()
    try:
        cursor = await db.execute("SELECT COUNT(*) FROM memory_metadata")
        (total,) = await cursor.fetchone()

        cursor = await db.execute(
            """
            SELECT COUNT(*) FROM memory_metadata m
            WHERE m.created_at < ?
              AND NOT EXISTS (
                  SELECT 1 FROM memory_links l
                  WHERE l.source_id = m.memory_id OR l.target_id = m.memory_id
              )
            """,
            (cutoff,),
        )
        (orphans,) = await cursor.fetchone()
    except aiosqlite.Error:
        logger.error("Failed to compute orphan stats", exc_info=True)
        return {"error": "DB unavailable"}

    return {
        "total_memories": total,
        "orphans": orphans,
        "orphan_pct": round(orphans / total * 100, 2) if total else 0.0,
    }


async def distribution_stats(db: aiosqlite.Connection) -> dict:
    """Counts by collection and top-20 tags from FTS5."""
    try:
        cursor = await db.execute(
            "SELECT collection, COUNT(*) FROM memory_metadata GROUP BY collection"
        )
        by_collection = {row[0]: row[1] for row in await cursor.fetchall()}

        cursor = await db.execute("SELECT COUNT(*) FROM memory_metadata")
        (total,) = await cursor.fetchone()
    except aiosqlite.Error:
        logger.error("Failed to compute distribution stats", exc_info=True)
        return {"error": "DB unavailable"}

    # Tag distribution from FTS5 — may not be available in all environments
    top_tags: list[tuple[str, int]] = []
    try:
        cursor = await db.execute("SELECT tags FROM memory_fts WHERE tags != ''")
        tag_counts: dict[str, int] = {}
        for (tags_str,) in await cursor.fetchall():
            if not tags_str:
                continue
            for tag in tags_str.split():
                tag = tag.strip().strip(",")
                if tag:
                    tag_counts[tag] = tag_counts.get(tag, 0) + 1
        top_tags = sorted(tag_counts.items(), key=lambda x: x[1], reverse=True)[:20]
    except aiosqlite.Error:
        logger.warning("memory_fts not available for tag distribution", exc_info=True)

    return {"by_collection": by_collection, "total": total, "top_tags": top_tags}


async def growth_stats(db: aiosqlite.Connection) -> dict:
    """Count memories created in recent time windows."""
    now = datetime.now(UTC)

    windows = {
        "last_24h": (now - timedelta(hours=24)).isoformat(),
        "last_7d": (now - timedelta(days=7)).isoformat(),
        "last_30d": (now - timedelta(days=30)).isoformat(),
    }
    result: dict[str, int | float] = {}

    try:
        for key, cutoff in windows.items():
            cursor = await db.execute(
                "SELECT COUNT(*) FROM memory_metadata WHERE created_at >= ?",
                (cutoff,),
            )
            (count,) = await cursor.fetchone()
            result[key] = count

        last_7d = result.get("last_7d", 0)
        result["avg_per_day_7d"] = round(last_7d / 7, 2) if isinstance(last_7d, int) else 0.0
    except aiosqlite.Error:
        logger.error("Failed to compute growth stats", exc_info=True)
        return {"error": "DB unavailable"}

    return result


async def full_health_report(
    db: aiosqlite.Connection,
    qdrant_client=None,
) -> dict:
    """Assemble a complete health report from all sub-checks."""
    report: dict[str, dict | None] = {
        "orphans": await orphan_stats(db),
        "distribution": await distribution_stats(db),
        "growth": await growth_stats(db),
        "duplicates": None,
    }

    if qdrant_client is not None:
        report["duplicates"] = await near_duplicate_stats(db, qdrant_client)

    return report
