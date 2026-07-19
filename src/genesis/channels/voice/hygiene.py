"""Daily voice hygiene — transcript aging + stale-producer blob sweep.

Two duties, both keyed on observable state, wired as the ``voice_hygiene``
retention job (``runtime/init/learning.py``):

1. ``prune_old_transcripts`` — delete voice transcript files older than the
   retention window (default 1 year, user decision 2026-07-19). The
   ``cc_sessions`` rows stay: they are the tiny topic/keyword index, matching
   how CC session rows outlive interest in their transcripts.

2. ``sweep_blob_memories`` — remove legacy "one-blob" voice conversation
   memories. Before the transcript-writer landing existed, both the core S2S
   manager and the edge s2s bridge stored each conversation as a single
   growing ``"Voice conversation [...]"`` blob in episodic memory (duplicated
   per client disconnect, cumulative across weeks). After this release
   nothing legitimate writes that signature, so any hit is a stale producer
   (an edge bridge that has not yet been updated) — swept daily and logged
   loudly. A one-shot data migration cannot do this job: the migration runner
   never re-runs completed migrations, so blobs written after the first purge
   would survive forever.

No-voice installs no-op for pennies: candidate discovery is an index-backed
FTS MATCH, not a content scan.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import aiosqlite

from genesis.db.crud import cc_sessions as sessions_crud
from genesis.env import voice_transcript_dir

if TYPE_CHECKING:
    from genesis.memory.store import MemoryStore

logger = logging.getLogger(__name__)

TRANSCRIPT_RETENTION_DAYS = 365

# Signature of the legacy blob landing. The tags MATCH narrows to a handful
# of candidates via the FTS index; the exact content prefix (checked in
# Python) is what makes a hit definitive — legitimate voice-tagged memories
# without the prefix survive. Live-verified against the polluted cohort
# 2026-07-19: FTS source_type is the generic 'memory' for these rows, so
# tags+prefix is the only reliable discriminator.
_BLOB_CANDIDATE_MATCH = "tags:voice AND tags:s2s AND tags:conversation"
_BLOB_CONTENT_PREFIX = "Voice conversation ["


async def prune_old_transcripts(
    db: aiosqlite.Connection,
    *,
    days: int = TRANSCRIPT_RETENTION_DAYS,
    transcript_dir: Path | None = None,
) -> int:
    """Delete voice transcript files past retention. Returns files removed.

    Age comes from the session row's ``started_at`` when one exists (file
    mtime as fallback for rowless files). A file whose session is still
    ``active`` is never deleted, regardless of age.
    """
    directory = transcript_dir or voice_transcript_dir()
    if not directory.is_dir():
        return 0

    cutoff = datetime.now(UTC) - timedelta(days=days)
    removed = 0
    for path in directory.glob("*.jsonl"):
        row = await sessions_crud.get_by_id(db, path.stem)
        if row is not None:
            if row.get("status") == "active":
                continue
            started_raw = row.get("started_at") or ""
            try:
                age_marker = datetime.fromisoformat(started_raw)
                if age_marker.tzinfo is None:
                    age_marker = age_marker.replace(tzinfo=UTC)
            except ValueError:
                age_marker = None
        else:
            age_marker = None

        if age_marker is None:
            try:
                age_marker = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
            except OSError:
                continue

        if age_marker < cutoff:
            try:
                path.unlink()
                removed += 1
            except OSError:
                logger.warning("Could not prune voice transcript %s", path, exc_info=True)

    if removed:
        logger.info(
            "Pruned %d voice transcript(s) older than %d days",
            removed,
            days,
        )
    return removed


async def sweep_blob_memories(
    db: aiosqlite.Connection,
    store: MemoryStore,
) -> int:
    """Cascade-delete legacy one-blob voice memories. Returns memories swept.

    Uses ``MemoryStore.delete`` (the real cascade: FTS, metadata, Qdrant both
    collections, links, pending embeddings, entity mentions) plus a
    ``memory_events`` delete — the one layer keyed by memory_id that
    ``store.delete`` does not cover.
    """
    cursor = await db.execute(
        "SELECT memory_id, content FROM memory_fts WHERE memory_fts MATCH ?",
        (_BLOB_CANDIDATE_MATCH,),
    )
    rows = await cursor.fetchall()

    swept = 0
    for row in rows:
        content = row["content"] or ""
        if not content.startswith(_BLOB_CONTENT_PREFIX):
            continue
        memory_id = row["memory_id"]
        await store.delete(memory_id)
        await db.execute(
            "DELETE FROM memory_events WHERE memory_id = ?",
            (memory_id,),
        )
        swept += 1

    if swept:
        await db.commit()
        # Loud by design: a nonzero sweep after the edge bridge is updated
        # means a blob producer is back — investigate, don't just clean up.
        logger.warning(
            "Voice hygiene swept %d legacy blob memor%s from episodic memory "
            "(stale producer still writing? edge bridge update pending?)",
            swept,
            "y" if swept == 1 else "ies",
        )
    return swept
