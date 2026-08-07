"""Algorithmic health checks on the Genesis memory store.

Pure SQL aggregates and Qdrant vector queries — NO LLM judgment.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import sqlite3
import time
from datetime import UTC, datetime, timedelta

import aiosqlite

from genesis.util.tasks import tracked_task

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
    sample_size: int = 20,
    threshold: float = 0.95,
) -> dict:
    """Sample memories and check Qdrant for near-duplicates above *threshold*.

    Uses sync Qdrant client calls wrapped in asyncio.to_thread to avoid
    blocking the event loop. Default sample_size is 20 to keep cost bounded
    (~40 Qdrant calls: scroll + search per sample).
    """

    try:
        cursor = await db.execute("SELECT memory_id FROM memory_metadata")
        all_ids = [row[0] for row in await cursor.fetchall()]
    except aiosqlite.Error:
        logger.error("Failed to query memory_metadata for duplicate check", exc_info=True)
        return {"error": "DB unavailable"}

    if not all_ids:
        return {"total_sampled": 0, "near_duplicates_found": 0, "pairs": []}

    sampled = random.sample(all_ids, min(sample_size, len(all_ids)))

    def _scan_duplicates() -> list[tuple[str, str, float]]:
        """Sync Qdrant work — runs in thread pool."""
        found: list[tuple[str, str, float]] = []
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
                    found.append((mem_id, hit_id, round(hit.score, 4)))
        return found

    try:
        pairs = await asyncio.to_thread(_scan_duplicates)
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


# ---------------------------------------------------------------------------
# top_tags: serve stale-while-revalidate (SWR)
#
# The top-tags computation scans the ENTIRE memory_fts corpus (one row per
# memory) and tag-counts it in pure Python. It is O(corpus) and was historically
# run INLINE on the caller's event loop — a multi-hundred-ms Python loop that
# grew with the corpus and chronically starved the genesis-server loop (recall
# 503s, lag WARNs). `top_tags` has ZERO production consumers, so it never needs
# to be fresh on the request path. It is now served stale-while-revalidate,
# mirroring `memory.intent.TagCooccurrenceIndex` (follow-up ac27b693): the cheap
# collection/total aggregates stay fresh on the caller connection; top_tags is
# served from a module cache that a single-flight background task refreshes
# off-loop (via a SEPARATE read-only sqlite connection) when stale.
# ---------------------------------------------------------------------------

_top_tags_cache: list[tuple[str, int]] = []
_top_tags_built_at: float = 0.0  # monotonic ts; 0.0 => never built
_top_tags_corpus_count: int = 0  # corpus size at last build (drives staleness)
_top_tags_rebuild_in_flight: bool = False  # single-flight guard for the bg task
_TAG_STATS_STALE_DELTA = 0.10  # rebuild when the corpus count changes by >10%
_TAG_STATS_MAX_AGE_S = 3600.0  # ...or when the cache is older than this


def _top_tags_disabled() -> bool:
    """Env kill switch — disables the background top_tags rebuild entirely."""
    return os.environ.get("GENESIS_TOP_TAGS_DISABLED", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _reset_top_tags_state() -> None:
    """Test seam: reset the module-level SWR state to a cold start."""
    global _top_tags_cache, _top_tags_built_at, _top_tags_corpus_count  # noqa: PLW0603
    global _top_tags_rebuild_in_flight  # noqa: PLW0603
    _top_tags_cache = []
    _top_tags_built_at = 0.0
    _top_tags_corpus_count = 0
    _top_tags_rebuild_in_flight = False


def _scan_top_tags(db_path: str, limit: int = 20) -> list[tuple[str, int]]:
    """Count the top *limit* tags from memory_fts on a fresh READ-ONLY connection.

    Runs OFF the event loop (inside ``asyncio.to_thread``), never on the caller
    path. Opens its own ``mode=ro`` connection (WAL-visible — NOT ``immutable=1``,
    which would miss un-checkpointed writes) so it never touches the shared
    aiosqlite write connection. Iterates the cursor directly (no ``fetchall``) so
    SQLite's C ``step`` releases the GIL between rows — the pure-Python counting
    holds the GIL only in microsecond slices instead of one multi-hundred-ms block.
    """
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.execute("PRAGMA busy_timeout=5000")
        cursor = conn.execute("SELECT tags FROM memory_fts WHERE tags != ''")
        tag_counts: dict[str, int] = {}
        for (tags_str,) in cursor:  # iterate directly — GIL released between rows
            if not tags_str:
                continue
            for tag in tags_str.split():
                tag = tag.strip().strip(",")
                if tag:
                    tag_counts[tag] = tag_counts.get(tag, 0) + 1
        return sorted(tag_counts.items(), key=lambda x: x[1], reverse=True)[:limit]
    except sqlite3.Error:
        logger.warning("memory_fts scan for top_tags failed", exc_info=True)
        return []
    finally:
        if conn is not None:
            conn.close()


async def _rebuild_top_tags(db_path: str, total: int) -> None:
    """Background SWR rebuild: scan tags in a thread, update the cache, clear flag.

    Single-flight — the scheduler sets ``_top_tags_rebuild_in_flight`` before
    scheduling this, and this clears it in a ``finally`` so a scan failure can't
    wedge the cache stale forever. The built-at / corpus markers are always set
    (even on an empty/failed scan) so a persistent FTS error can't hot-loop the
    scheduler; the cache simply refreshes on the next staleness window.
    """
    global _top_tags_cache, _top_tags_built_at, _top_tags_corpus_count  # noqa: PLW0603
    global _top_tags_rebuild_in_flight  # noqa: PLW0603
    try:
        _top_tags_cache = await asyncio.to_thread(_scan_top_tags, db_path)
        _top_tags_built_at = time.monotonic()
        _top_tags_corpus_count = total
        logger.info(
            "top_tags cache rebuilt in background (%d tags, corpus=%d)",
            len(_top_tags_cache),
            total,
        )
    finally:
        _top_tags_rebuild_in_flight = False


def _top_tags_is_stale(total: int) -> bool:
    """True when the cache needs a rebuild (never built, corpus drift, or age).

    "Ever built" is tracked by ``_top_tags_built_at != 0.0`` ALONE — NOT by a
    non-zero corpus count, so a genuinely empty corpus that has been built reads
    as fresh (the delta check below handles a later 0 → N growth via max(…, 1)).
    """
    if _top_tags_built_at == 0.0:
        return True
    delta = abs(total - _top_tags_corpus_count) / max(_top_tags_corpus_count, 1)
    if delta > _TAG_STATS_STALE_DELTA:
        return True
    return (time.monotonic() - _top_tags_built_at) > _TAG_STATS_MAX_AGE_S


async def _main_db_path(db: aiosqlite.Connection) -> str | None:
    """Resolve the main database file path for the out-of-band read-only scan.

    Returns None for ``:memory:`` databases (tests) — a separate connection
    cannot see an in-memory DB, so the rebuild is skipped gracefully.
    """
    try:
        cursor = await db.execute("PRAGMA database_list")
        for row in await cursor.fetchall():
            # PRAGMA database_list columns: (seq, name, file)
            if row[1] == "main":
                return row[2] or None
    except aiosqlite.Error:
        logger.warning("Failed to resolve main db path for top_tags scan", exc_info=True)
    return None


async def _maybe_refresh_top_tags(db: aiosqlite.Connection, total: int) -> None:
    """Schedule a single-flight background top_tags rebuild if the cache is stale."""
    global _top_tags_rebuild_in_flight  # noqa: PLW0603
    if _top_tags_disabled() or _top_tags_rebuild_in_flight:
        return
    if not _top_tags_is_stale(total):
        return
    # Claim single-flight BEFORE the first await (_main_db_path yields on the
    # SerializedConnection lock). The in-flight guard above and this set have no
    # await between them, so under cooperative scheduling a second concurrent
    # caller sees the flag set and bails — no double-schedule. Once the rebuild is
    # actually scheduled, ownership of clearing the flag transfers to
    # _rebuild_top_tags's finally; until then, THIS finally clears it — including
    # on CancelledError (BaseException, so `except Exception` would miss it),
    # which the widened flag-held window across the await now makes reachable
    # (wait_for timeouts, DB-lock retries, restart cancellation).
    _top_tags_rebuild_in_flight = True
    scheduled = False
    try:
        db_path = await _main_db_path(db)
        if db_path is None:
            return  # :memory: (tests) — no out-of-band scan possible
        tracked_task(_rebuild_top_tags(db_path, total), name="top_tags_rebuild")
        scheduled = True
    except Exception:
        # Resolution or scheduling failed — flag cleared in the finally below.
        logger.warning("Failed to schedule top_tags rebuild", exc_info=True)
    finally:
        if not scheduled:
            _top_tags_rebuild_in_flight = False


async def distribution_stats(db: aiosqlite.Connection) -> dict:
    """Counts by collection and total (fresh) + top-20 tags (served stale).

    The collection/total aggregates are cheap and computed fresh on the caller
    connection. ``top_tags`` is served from the SWR cache above; a stale cache
    triggers a single-flight background refresh (off-loop) but this call always
    returns immediately with whatever the cache currently holds. ``top_tags`` has
    no production consumers, so staleness is invisible; freshness is never worth
    an O(corpus) FTS scan on the event loop.
    """
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

    await _maybe_refresh_top_tags(db, total)

    return {
        "by_collection": by_collection,
        "total": total,
        "top_tags": list(_top_tags_cache),
        "top_tags_warming": _top_tags_built_at == 0.0,
    }


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
