"""Periodic memory extraction job.

Reads active session transcripts, extracts entities/decisions/relationships,
and stores them in the memory system with provenance.  Runs every 1-2 hours
via the surplus scheduler.

Extraction scope:
- Foreground sessions (user conversations)
- Inbox evaluation sessions (background CC that evaluates URLs)
- Excluded: reflection, surplus, bridge sessions (have their own pipelines)
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import aiosqlite

from genesis.env import cc_project_dir
from genesis.memory.extraction import (
    RETRY_PROMPT,
    ExtractionResult,
    build_extraction_prompt,
    extractions_to_store_kwargs,
    parse_extraction_response_full,
)
from genesis.memory.reference_extraction import extract_references_from_chunk
from genesis.util.jsonl import (
    chunk_messages,
    format_chunk_for_extraction,
    read_transcript_messages,
)

if TYPE_CHECKING:
    from genesis.memory.linker import MemoryLinker
    from genesis.memory.store import MemoryStore
    from genesis.routing.router import Router

logger = logging.getLogger(__name__)

# Session types eligible for extraction
_EXTRACTABLE_SOURCE_TAGS = {"foreground", "inbox"}

# Transcript directory
_TRANSCRIPT_DIR = Path.home() / ".claude" / "projects" / cc_project_dir()


async def run_extraction_cycle(
    *,
    db: aiosqlite.Connection,
    store: MemoryStore,
    router: Router,
    linker: MemoryLinker | None = None,
    transcript_dir: Path = _TRANSCRIPT_DIR,
    chunk_size: int = 50,
    max_retries: int = 2,
    reference_only_mode: bool = False,
    start_line_override: int | None = None,
    session_filter: set[str] | None = None,
    max_extractions_per_session: int = 30,
) -> dict:
    """Run one extraction cycle across all eligible sessions.

    Returns a summary dict with counts for observability.

    ``reference_only_mode`` (default False): when True, the cycle runs the
    LLM extraction AND reference classifier, but SKIPS episodic memory
    storage, watermark updates, and session-index updates. Used by the
    one-shot history mining CLI to pull reference data out of historical
    transcripts without polluting episodic memory with duplicates.

    ``start_line_override`` (default None): when set, ignores the per-session
    ``last_extracted_line`` watermark and starts from this line instead.
    Combined with ``reference_only_mode=True``, this lets the history mining
    script re-read transcripts from the beginning without touching production
    extraction state.

    ``session_filter`` (default None): when set, only process sessions whose
    id is in this set. Used to scope history mining to specific sessions.
    """
    summary = {
        "sessions_processed": 0,
        "chunks_processed": 0,
        "entities_extracted": 0,
        "references_captured": 0,
        "zero_entity_chunks": 0,
        "errors": 0,
    }

    # Find sessions with unextracted content (includes filesystem discovery)
    sessions = await _find_extractable_sessions(db, transcript_dir=transcript_dir)
    if session_filter is not None:
        sessions = [s for s in sessions if s["id"] in session_filter]

    for session in sessions:
        session_id = session["id"]
        cc_session_id = session.get("cc_session_id") or session_id
        if start_line_override is not None:
            last_line = start_line_override
        else:
            last_line = session.get("last_extracted_line") or 0
        transcript_path = _find_transcript(transcript_dir, cc_session_id)

        if not transcript_path:
            continue

        # Read new messages since last extraction
        messages = read_transcript_messages(
            transcript_path,
            start_line=last_line,
        )
        if not messages:
            continue

        chunks = chunk_messages(messages, chunk_size=chunk_size)
        max_line = last_line
        all_keywords: set[str] = set()
        latest_topic = ""
        session_extraction_count = 0

        for chunk in chunks:
            chunk_start = chunk[0].line_number
            chunk_end = chunk[-1].line_number
            max_line = max(max_line, chunk_end + 1)

            result = await _extract_chunk(
                chunk=chunk,
                router=router,
                max_retries=max_retries,
            )
            summary["chunks_processed"] += 1

            if result.parse_error:
                summary["errors"] += 1
                logger.error(
                    "Extraction parse error for session %s chunk %d-%d: %s",
                    session_id, chunk_start, chunk_end, result.parse_error,
                )
                continue

            # Accumulate session-level keywords and topic from each chunk
            if result.session_keywords:
                all_keywords.update(result.session_keywords)
            if result.session_topic:
                latest_topic = result.session_topic

            if not result.extractions:
                summary["zero_entity_chunks"] += 1
                logger.warning(
                    "Zero entities extracted from session %s chunk %d-%d "
                    "(possible extraction quality issue)",
                    session_id, chunk_start, chunk_end,
                )
                continue

            # Silent auto-capture: classify each extraction for reference
            # shape (credentials, URLs, IPs, etc.) and promote matches to
            # the reference store. Runs in BOTH normal and reference-only
            # modes — this is the dominant path that populates the
            # reference store from historical conversations.
            try:
                ref_count = await extract_references_from_chunk(
                    result.extractions,
                    store=store,
                    db=db,
                    source_session_id=cc_session_id,
                    # Normal: queue for paced embedding; history mining: embed
                    # inline (one-shot CLI won't have recovery worker running)
                    force_fts5_only=not reference_only_mode,
                )
                summary["references_captured"] += ref_count
            except Exception:
                # Reference extraction must never break the main extraction
                # pipeline — log and continue.
                logger.warning(
                    "Reference extractor failed on session %s chunk %d-%d",
                    session_id, chunk_start, chunk_end, exc_info=True,
                )

            if reference_only_mode:
                # Skip the episodic storage loop — history mining uses this
                # path to populate the reference store without duplicating
                # episodic memory rows that already exist from prior cycles.
                continue

            # Store each extraction with provenance
            for extraction in result.extractions:
                kwargs = extractions_to_store_kwargs(
                    extraction,
                    source_session_id=cc_session_id,
                    transcript_path=str(transcript_path),
                    source_line_range=(chunk_start, chunk_end),
                )
                try:
                    # Queue-first: store FTS5-only, queue embedding for
                    # the recovery worker's paced drain. Prevents the
                    # extraction cycle from hammering the embedding backend
                    # with hundreds of sequential calls.
                    memory_id = await store.store(
                        **kwargs, force_fts5_only=True,
                    )
                    summary["entities_extracted"] += 1
                    session_extraction_count += 1

                    # Store SVO event if temporal + verb present
                    if extraction.event_verb and extraction.temporal:
                        try:
                            from genesis.db.crud import memory_events
                            await memory_events.insert(
                                db,
                                memory_id=memory_id,
                                subject=extraction.event_subject or "unknown",
                                verb=extraction.event_verb,
                                object=extraction.event_object,
                                event_date=extraction.temporal,
                                confidence=extraction.confidence,
                                source_session_id=cc_session_id,
                            )
                        except Exception:
                            logger.warning(
                                "Failed to store SVO event for %s",
                                memory_id, exc_info=True,
                            )

                    # Create typed links from extraction relationships
                    if linker and extraction.relationships:
                        try:
                            await linker.create_typed_links(
                                memory_id, extraction.relationships,
                            )
                        except Exception:
                            logger.error(
                                "Failed to create typed links for %s",
                                memory_id, exc_info=True,
                            )
                except Exception:
                    summary["errors"] += 1
                    logger.error(
                        "Failed to store extraction from session %s",
                        session_id, exc_info=True,
                    )

            # Check per-session cap after storing this chunk's extractions
            if session_extraction_count >= max_extractions_per_session:
                logger.info(
                    "Hit per-session extraction cap (%d) for session %s, "
                    "skipping remaining chunks",
                    max_extractions_per_session, session_id,
                )
                break

        # Update watermark + session keywords/topic
        # Skip both in reference_only_mode so the history mining run leaves
        # production extraction state untouched — the next regular cycle
        # will still pick up the same transcripts for episodic storage.
        if not reference_only_mode:
            await _update_watermark(db, session_id, max_line)
            if all_keywords or latest_topic:
                await _update_session_index(
                    db, session_id,
                    keywords=all_keywords, topic=latest_topic,
                )
        summary["sessions_processed"] += 1

    return summary


async def _find_extractable_sessions(
    db: aiosqlite.Connection,
    transcript_dir: Path = _TRANSCRIPT_DIR,
) -> list[dict]:
    """Find sessions eligible for extraction with unprocessed content.

    Uses a hybrid approach:
    1. DB-registered sessions (from bridge/channel pathway)
    2. Filesystem discovery — scan transcript dir for .jsonl files not yet
       registered, and auto-register them as foreground sessions.

    This ensures interactive CLI sessions (which bypass cc_sessions registration)
    are still discoverable for extraction.
    """
    # Phase 1: Auto-register untracked transcripts from filesystem
    if transcript_dir.is_dir():
        try:
            known_cursor = await db.execute(
                "SELECT cc_session_id FROM cc_sessions WHERE cc_session_id IS NOT NULL"
            )
            known_ids = {row[0] for row in await known_cursor.fetchall()}

            for jsonl_file in transcript_dir.glob("*.jsonl"):
                session_id = jsonl_file.stem
                # Skip non-UUID filenames and already-registered sessions
                if len(session_id) < 32 or session_id in known_ids:
                    continue
                # Auto-register as foreground session
                import uuid as _uuid
                try:
                    _uuid.UUID(session_id)  # validate UUID format
                except ValueError:
                    continue

                # Get file mtime as approximate start time
                mtime = datetime.fromtimestamp(
                    jsonl_file.stat().st_mtime, tz=UTC,
                )
                mtime_iso = mtime.isoformat()
                await db.execute(
                    "INSERT OR IGNORE INTO cc_sessions "
                    "(id, cc_session_id, session_type, model, source_tag, "
                    " status, started_at, last_activity_at) "
                    "VALUES (?, ?, 'foreground', 'unknown', 'foreground', "
                    " 'completed', ?, ?)",
                    (session_id, session_id, mtime_iso, mtime_iso),
                )
            await db.commit()
        except Exception:
            logger.warning(
                "Filesystem transcript discovery failed — falling back to DB-only",
                exc_info=True,
            )

    # Phase 2: Query all extractable sessions (including newly registered ones)
    cursor = await db.execute(
        """
        SELECT id, cc_session_id, source_tag, last_extracted_at,
               last_extracted_line, started_at
        FROM cc_sessions
        WHERE source_tag IN ({})
          AND status IN ('active', 'completed', 'checkpointed')
        ORDER BY started_at DESC
        """.format(",".join("?" for _ in _EXTRACTABLE_SOURCE_TAGS)),
        tuple(_EXTRACTABLE_SOURCE_TAGS),
    )
    rows = await cursor.fetchall()
    columns = [d[0] for d in cursor.description]
    return [dict(zip(columns, row, strict=True)) for row in rows]


async def _update_watermark(
    db: aiosqlite.Connection,
    session_id: str,
    line_number: int,
) -> None:
    """Update the extraction watermark for a session."""
    now_iso = datetime.now(UTC).isoformat()
    await db.execute(
        "UPDATE cc_sessions SET last_extracted_at = ?, last_extracted_line = ? "
        "WHERE id = ?",
        (now_iso, line_number, session_id),
    )
    await db.commit()


async def _update_session_index(
    db: aiosqlite.Connection,
    session_id: str,
    *,
    keywords: set[str],
    topic: str,
) -> None:
    """Update session topic and keywords for structured search.

    Keywords are accumulated across chunks (deduplicated). Topic is the
    latest chunk's topic (most recent = most complete context).
    Appends to existing keywords rather than overwriting.
    """
    # Read existing keywords to merge
    cursor = await db.execute(
        "SELECT keywords FROM cc_sessions WHERE id = ?", (session_id,),
    )
    row = await cursor.fetchone()
    existing = set()
    if row and row[0]:
        existing = {k.strip() for k in row[0].split(",") if k.strip()}

    merged = sorted(existing | keywords)
    keywords_str = ", ".join(merged)

    await db.execute(
        "UPDATE cc_sessions SET topic = ?, keywords = ? WHERE id = ?",
        (topic, keywords_str, session_id),
    )
    await db.commit()
    logger.info(
        "Session %s indexed: topic=%r, keywords=%d",
        session_id[:8], topic[:60], len(merged),
    )


def _find_transcript(transcript_dir: Path, cc_session_id: str) -> Path | None:
    """Find the JSONL transcript file for a CC session ID.

    CC stores transcripts as {session_id}.jsonl in the project directory.
    Also check for transcripts in session-specific subdirectories.
    """
    # Path traversal protection: validate session ID doesn't escape directory
    resolved_dir = transcript_dir.resolve()

    # Direct file: {session_id}.jsonl
    direct = transcript_dir / f"{cc_session_id}.jsonl"
    if direct.exists():
        if not str(direct.resolve()).startswith(str(resolved_dir)):
            logger.warning("Path traversal attempt blocked: %s", cc_session_id)
            return None
        return direct

    # Subdirectory: {session_id}/{session_id}.jsonl or similar
    subdir = transcript_dir / cc_session_id
    if subdir.is_dir():
        if not str(subdir.resolve()).startswith(str(resolved_dir)):
            logger.warning("Path traversal attempt blocked: %s", cc_session_id)
            return None
        for jsonl in subdir.glob("*.jsonl"):
            return jsonl

    return None


async def _extract_chunk(
    *,
    chunk: list,
    router: Router,
    max_retries: int = 2,
) -> ExtractionResult:
    """Extract entities from a conversation chunk via LLM.

    Uses router call site #9 (fact_extraction) with retry on parse failure.
    """
    conversation_text = format_chunk_for_extraction(chunk)
    prompt = build_extraction_prompt(conversation_text)

    chunk_start = chunk[0].line_number
    chunk_end = chunk[-1].line_number

    for attempt in range(max_retries):
        try:
            if attempt == 0:
                messages = [{"role": "user", "content": prompt}]
            else:
                messages = [
                    {"role": "user", "content": prompt},
                    {"role": "assistant", "content": "(previous attempt failed to produce valid JSON)"},
                    {"role": "user", "content": RETRY_PROMPT},
                ]

            response = await router.route_call(
                call_site_id="9_fact_extraction",
                messages=messages,
            )

            if not response.success:
                logger.warning(
                    "Router call failed for extraction: %s",
                    response.error,
                )
                return ExtractionResult(
                    extractions=[],
                    chunk_line_start=chunk_start,
                    chunk_line_end=chunk_end,
                    parse_error=response.error or "Router call failed",
                )

            text = response.content or ""
            parsed = parse_extraction_response_full(text)

            return ExtractionResult(
                extractions=parsed.extractions,
                chunk_line_start=chunk_start,
                chunk_line_end=chunk_end,
                raw_response=text,
                session_keywords=parsed.session_keywords,
                session_topic=parsed.session_topic,
            )

        except ValueError as exc:
            if attempt < max_retries - 1:
                logger.warning(
                    "Extraction parse failed (attempt %d/%d): %s",
                    attempt + 1, max_retries, exc,
                )
                continue
            return ExtractionResult(
                extractions=[],
                chunk_line_start=chunk_start,
                chunk_line_end=chunk_end,
                raw_response=text if "text" in locals() else None,
                parse_error=str(exc),
            )
        except Exception as exc:
            logger.error(
                "Extraction LLM call failed: %s", exc, exc_info=True,
            )
            return ExtractionResult(
                extractions=[],
                chunk_line_start=chunk_start,
                chunk_line_end=chunk_end,
                parse_error=str(exc),
            )

    # Should not reach here, but safety return
    return ExtractionResult(
        extractions=[],
        chunk_line_start=chunk_start,
        chunk_line_end=chunk_end,
        parse_error="Exhausted retries",
    )
