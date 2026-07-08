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

import contextlib
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import aiosqlite

from genesis.env import cc_project_dir
from genesis.learning.procedural.judge import ProcedureBuilderUnavailable
from genesis.memory.extraction import (
    RETRY_PROMPT,
    ExtractionResult,
    build_extraction_prompt,
    extractions_to_store_kwargs,
    parse_extraction_response_full,
)
from genesis.memory.reference_extraction import extract_references_from_chunk
from genesis.memory.source_verification import compute_jaccard, verify_source_overlap
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

# Durable rebuild queue for procedure builds that died on provider exhaustion.
# The extraction watermark advances BEFORE the whole-session procedure builder
# runs, so once a build fails the session is never revisited by the normal cycle
# (``if not messages: continue``) and the procedure is lost for good. On failure
# the session is re-queued here and a later cycle rebuilds it from the full
# transcript. Capped so a persistently-failing session (e.g. a rotated
# transcript) can't retry forever — exhaustion is surfaced as an observation,
# never silently dropped.
_PROCEDURE_REBUILD_WORK = "procedure_rebuild"
_MAX_PROCEDURE_REBUILD_ATTEMPTS = 8


async def _check_claim_duplicate(
    db: aiosqlite.Connection,
    content: str,
    *,
    jaccard_threshold: float = 0.70,
    fts5_limit: int = 10,
) -> bool:
    """Check if content is a near-duplicate of an existing memory.

    Uses FTS5 keyword search to find candidates, then Jaccard overlap
    to confirm. Deduplicates across all sessions.
    """
    import re
    # Use a simple alphanumeric tokenizer inline rather than importing
    # the private _extract_terms. Avoids cross-module private API coupling.
    words = re.findall(r"[a-z0-9]+", content.lower())
    terms = {w for w in words if len(w) > 2}
    if len(terms) < 3:
        return False  # Too short to meaningfully dedup

    # Build FTS5 query from top terms. Only use pure alphanumeric terms —
    # hyphens are FTS5 NOT operators, apostrophes cause parse errors.
    query_terms = sorted(terms, key=len, reverse=True)[:5]
    fts_query = " OR ".join(query_terms)

    try:
        cursor = await db.execute(
            "SELECT memory_id, content FROM memory_fts "
            "WHERE memory_fts MATCH ? LIMIT ?",
            (fts_query, fts5_limit),
        )
        rows = await cursor.fetchall()
    except Exception:
        return False

    for _memory_id, existing_content in rows:
        if compute_jaccard(content, existing_content) >= jaccard_threshold:
            return True

    return False


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
    max_procedures_per_session: int = 7,
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
        "events_stored": 0,
        "events_failed": 0,
        "goals_detected": 0,
        "contacts_detected": 0,
        "connections_created": 0,
        "errors": 0,
    }
    # Collect newly stored memories for cross-session connection discovery
    _newly_stored: list[tuple[str, str, str]] = []

    # Rebuild procedures whose build previously died on provider exhaustion.
    # Their sessions' watermarks have advanced, so the loop below will skip them
    # (``if not messages: continue``) — this drain is the only path that recovers
    # them. Skipped for the history-mining / filtered invocations, which don't
    # own production extraction state.
    if (
        not reference_only_mode
        and start_line_override is None
        and session_filter is None
    ):
        try:
            await _drain_procedure_rebuilds(
                db=db,
                router=router,
                transcript_dir=transcript_dir,
                summary=summary,
                max_procedures_per_session=max_procedures_per_session,
            )
        except Exception:
            logger.error("Procedure rebuild drain failed", exc_info=True)

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
        # Per-session cap on NEW draft procedures (extraction + struggle
        # streams). Without this, a single session can flood the store with
        # hundreds of conf≈0 candidates that never get validated.
        session_procs = 0
        session_candidates: list[dict] = []  # flag-only chunk signal (C2b)

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
                # Gate reference auto-capture to the USER's own words — the
                # classifier must not mine Genesis's analysis prose (the
                # dominant source of junk refs) for credentials/IPs/URLs.
                chunk_user_text = "\n".join(
                    m.text for m in chunk if m.role == "user"
                )
                ref_count = await extract_references_from_chunk(
                    result.extractions,
                    store=store,
                    db=db,
                    source_session_id=cc_session_id,
                    # Normal: queue for paced embedding; history mining: embed
                    # inline (one-shot CLI won't have recovery worker running)
                    force_fts5_only=not reference_only_mode,
                    user_text=chunk_user_text,
                )
                summary["references_captured"] += ref_count
            except Exception:
                # Reference extraction must never break the main extraction
                # pipeline — log and continue.
                logger.warning(
                    "Reference extractor failed on session %s chunk %d-%d",
                    session_id, chunk_start, chunk_end, exc_info=True,
                )

            # Stream 2: procedure candidate flagging (C2b — flag-only). The SLM
            # flags procedure_candidate extractions; we classify them and
            # ACCUMULATE them as a session-level signal. No Judge call and no
            # storage here — the whole-session builder (Stream 1) does the
            # reconstruction. This fixes the prior priority inversion, where the
            # chunk path could exhaust the per-session cap before the
            # higher-fidelity struggle builder ran.
            if not reference_only_mode:
                try:
                    from genesis.memory.procedure_extraction import (
                        extract_procedures_from_chunk,
                    )

                    session_candidates.extend(
                        extract_procedures_from_chunk(result.extractions)
                    )
                except Exception:
                    logger.warning(
                        "Procedure candidate flagging failed on session %s chunk %d-%d",
                        session_id, chunk_start, chunk_end, exc_info=True,
                    )

            if reference_only_mode:
                # Skip the episodic storage loop — history mining uses this
                # path to populate the reference store without duplicating
                # episodic memory rows that already exist from prior cycles.
                continue

            # Build source text once per chunk for overlap verification
            source_text = format_chunk_for_extraction(chunk)

            # Store each extraction with provenance
            for extraction in result.extractions:
                # procedure_candidate extractions are routed to the Judge
                # (Stream 2 above). Don't also store them as episodic memory.
                if extraction.extraction_type == "procedure_candidate":
                    continue

                # ── Source-overlap verification ──
                # Check that extraction content actually appears in the
                # source transcript chunk. Demote confidence for ungrounded
                # extractions. See memory-immune-system-design.md §1.1.
                overlap_result = verify_source_overlap(
                    extraction.content, source_text,
                )
                if not overlap_result.verified:
                    extraction.confidence = max(
                        extraction.confidence - 0.3, 0.1,
                    )
                    logger.info(
                        "Source-overlap FAIL for extraction in session %s "
                        "(overlap=%.2f, confidence demoted to %.2f): %.80s",
                        session_id, overlap_result.overlap,
                        extraction.confidence, extraction.content,
                    )

                # ── Cross-session claim dedup ──
                # Check if a highly similar extraction already exists.
                # Uses FTS5 keyword search + Jaccard overlap.
                # See memory-immune-system-design.md §1.2.
                try:
                    is_dup = await _check_claim_duplicate(
                        db, extraction.content,
                    )
                    if is_dup:
                        summary.setdefault("claims_deduped", 0)
                        summary["claims_deduped"] += 1
                        logger.info(
                            "Cross-session dedup: skipping extraction in "
                            "session %s (duplicate of existing memory): %.80s",
                            session_id, extraction.content,
                        )
                        continue  # Skip this extraction entirely
                except Exception:
                    logger.debug("Claim dedup check failed", exc_info=True)

                kwargs = extractions_to_store_kwargs(
                    extraction,
                    source_session_id=cc_session_id,
                    transcript_path=str(transcript_path),
                    source_line_range=(chunk_start, chunk_end),
                )
                # Tag unverified extractions for observability
                if not overlap_result.verified:
                    kwargs["tags"].append("source_unverified")

                try:
                    # Queue-first: store FTS5-only, queue embedding for
                    # the recovery worker's paced drain. Prevents the
                    # extraction cycle from hammering the embedding backend
                    # with hundreds of sequential calls.
                    memory_id = await store.store(
                        **kwargs, force_fts5_only=True,
                    )
                    summary["entities_extracted"] += 1
                    # Count source-unverified only for stored extractions
                    if not overlap_result.verified:
                        summary.setdefault("source_unverified", 0)
                        summary["source_unverified"] += 1
                    session_extraction_count += 1
                    _newly_stored.append(
                        (memory_id, extraction.content, cc_session_id)
                    )

                    # Store SVO event if temporal + verb present
                    if extraction.event_verb and extraction.temporal:
                        try:
                            from genesis.db.crud import memory_events
                            await memory_events.insert(
                                db,
                                memory_id=memory_id,
                                subject=extraction.event_subject or "unknown",
                                verb=extraction.event_verb,
                                object_=extraction.event_object,
                                event_date=extraction.temporal,
                                confidence=extraction.confidence,
                                source_session_id=cc_session_id,
                                _commit=False,
                            )
                            summary["events_stored"] += 1
                        except Exception:
                            summary["events_failed"] += 1
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

                    # Goal signal detection — DISABLED: keyword matcher
                    # produced ~95% false positives (277 garbage goals from
                    # conversation snippets). Replaced by explicit goal
                    # creation via MCP tool / foreground session.
                    # See PR for context; re-enable when goal_tracker uses
                    # LLM classification instead of keyword matching.

                    # Contact detection from person entities
                    try:
                        from genesis.memory.contact_tracker import (
                            process_extraction as _track_contact,
                        )
                        n = await _track_contact(
                            db, extraction,
                            source_session_id=cc_session_id,
                        )
                        summary["contacts_detected"] += n
                    except Exception:
                        logger.debug(
                            "Contact tracker failed for %s",
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

            # Stream 1: the whole-session procedure BUILDER (runs once per
            # session, after all chunks). Parses the full JSONL into a user-blind
            # action spine + a grounding haystack, scores struggle, and runs the
            # multi-procedure builder when EITHER the struggle score crosses the
            # threshold OR the chunk path flagged candidates. Returns 0..N
            # topic-segmented playbooks.
            try:
                import asyncio

                from genesis.learning.procedural.struggle_detector import (
                    STRUGGLE_THRESHOLD,
                    build_spine_and_haystack,
                    score_struggle,
                )

                spine, haystack = build_spine_and_haystack(transcript_path)
                struggle_score = score_struggle(spine)
                struggle_triggered = struggle_score >= STRUGGLE_THRESHOLD
                if (
                    (struggle_triggered or session_candidates)
                    and session_procs < max_procedures_per_session
                ):
                    from genesis.learning.procedural.judge import (
                        JUDGE_TIMEOUT_SECS,
                        judge_multi_procedure,
                    )

                    stored_ids = await asyncio.wait_for(
                        judge_multi_procedure(
                            db, spine, haystack, struggle_score, router,
                            source_session_id=cc_session_id,
                            max_new=max_procedures_per_session - session_procs,
                        ),
                        timeout=JUDGE_TIMEOUT_SECS,
                    )
                    session_procs += len(stored_ids)
                    if stored_ids:
                        summary["struggle_procedures"] = (
                            summary.get("struggle_procedures", 0) + len(stored_ids)
                        )
                    # Measure SLM over-flagging: the builder fired on chunk
                    # candidates alone, with no struggle signal.
                    if session_candidates and not struggle_triggered:
                        logger.info(
                            "Procedure builder fired on candidates-only for %s "
                            "(%d candidates, score=%.2f): stored=%d",
                            session_id, len(session_candidates), struggle_score,
                            len(stored_ids),
                        )
                    logger.info(
                        "Procedure builder for %s: score=%.2f, candidates=%d, stored=%d",
                        session_id, struggle_score, len(session_candidates),
                        len(stored_ids),
                    )
            except (ProcedureBuilderUnavailable, TimeoutError) as exc:
                # Transient: the provider chain was exhausted, or the build was
                # cancelled mid-flight (timeout). Re-queue the session for a later
                # rebuild — the watermark has already advanced past its lines
                # above, so without this the session is never revisited and the
                # procedure is lost. Do NOT db.rollback(): this ``db`` is the
                # shared SerializedConnection and a rollback discards EVERY
                # coroutine's pending write, not just ours (see connection.py).
                # There is nothing of ours to undo anyway — procedure stores
                # self-commit and the deferred memory_events was committed by the
                # watermark update above.
                logger.warning(
                    "Procedure builder unavailable for session %s (%s) — "
                    "re-queuing for rebuild", session_id, type(exc).__name__,
                )
                await _enqueue_procedure_rebuild(db, session_id, cc_session_id)
            except Exception:
                # Deterministic failure (parse/logic) — NOT re-queued; retrying
                # would burn every attempt identically. No rollback (shared
                # connection — see above).
                logger.warning(
                    "Procedure builder failed for session %s",
                    session_id, exc_info=True,
                )

        summary["sessions_processed"] += 1

    # Cross-session connection discovery (vector-based, no LLM)
    if _newly_stored and not reference_only_mode:
        try:
            from genesis.memory.connection_pass import run_connection_pass

            conn_result = await run_connection_pass(
                db=db,
                qdrant_client=store.qdrant_client,
                embedding_provider=store.embedding_provider,
                newly_stored=_newly_stored,
            )
            summary["connections_created"] = conn_result["connections_created"]
        except Exception:
            logger.warning("Connection pass failed", exc_info=True)

    return summary


async def _enqueue_procedure_rebuild(
    db: aiosqlite.Connection, session_id: str, cc_session_id: str,
) -> None:
    """Re-queue a session whose procedure build died on provider exhaustion.

    Durable (deferred_work_queue): a later extraction cycle drains it and rebuilds
    the procedures from the full transcript. Best-effort — a failure to enqueue
    must not break the extraction cycle. Rebuild idempotency is guaranteed by the
    novelty/dedup gate in ``store_procedure_checked``.
    """
    from genesis.resilience.deferred_work import (
        DRAIN,
        MEMORY_OPS,
        DeferredWorkQueue,
    )

    try:
        queue = DeferredWorkQueue(db)
        await queue.enqueue(
            work_type=_PROCEDURE_REBUILD_WORK,
            call_site_id="38_procedure_extraction",
            priority=MEMORY_OPS,
            payload=json.dumps(
                {"session_id": session_id, "cc_session_id": cc_session_id}
            ),
            reason="procedure builder provider-exhausted; watermark already advanced",
            staleness_policy=DRAIN,
        )
    except Exception:
        logger.error(
            "Failed to enqueue procedure rebuild for session %s",
            session_id, exc_info=True,
        )


async def _drain_procedure_rebuilds(
    *,
    db: aiosqlite.Connection,
    router: Router,
    transcript_dir: Path,
    summary: dict,
    max_procedures_per_session: int,
) -> None:
    """Rebuild procedures for sessions whose build previously died.

    Per queued item: rebuild spine+haystack from the full transcript and re-run
    the whole-session builder. Success → mark completed (even with zero
    procedures — the provider was available, nothing to rebuild). Still
    provider-exhausted → reset to pending (retry next cycle). Transcript gone or
    attempts exhausted → discard, surfacing an observation so the genuine loss is
    visible, never silent.
    """
    import asyncio

    from genesis.db.crud import deferred_work as dw_crud
    from genesis.learning.procedural.judge import (
        JUDGE_TIMEOUT_SECS,
        judge_multi_procedure,
    )
    from genesis.learning.procedural.struggle_detector import (
        build_spine_and_haystack,
        score_struggle,
    )
    from genesis.resilience.deferred_work import DeferredWorkQueue

    queue = DeferredWorkQueue(db)
    items = await dw_crud.query_pending(
        db, work_type=_PROCEDURE_REBUILD_WORK, limit=20,
    )
    for item in items:
        item_id = item["id"]
        attempts = item.get("attempts", 0)

        if attempts >= _MAX_PROCEDURE_REBUILD_ATTEMPTS:
            await _exhaust_procedure_rebuild(db, queue, item)
            continue

        try:
            payload = json.loads(item.get("payload_json", "{}"))
        except (json.JSONDecodeError, TypeError):
            await queue.mark_discarded(item_id, "unparseable payload")
            continue

        cc_session_id = payload.get("cc_session_id") or payload.get("session_id")
        transcript_path = (
            _find_transcript(transcript_dir, cc_session_id) if cc_session_id else None
        )
        if not transcript_path:
            # Transcript rotated/gone — nothing to rebuild from.
            await queue.mark_discarded(item_id, "transcript not found")
            continue

        await queue.mark_processing(item_id)  # increments attempts
        try:
            spine, haystack = build_spine_and_haystack(transcript_path)
            score = score_struggle(spine)
            stored_ids = await asyncio.wait_for(
                judge_multi_procedure(
                    db, spine, haystack, score, router,
                    source_session_id=cc_session_id,
                    max_new=max_procedures_per_session,
                ),
                timeout=JUDGE_TIMEOUT_SECS,
            )
            await queue.mark_completed(item_id)
            summary["procedures_rebuilt"] = (
                summary.get("procedures_rebuilt", 0) + len(stored_ids)
            )
            if stored_ids:
                logger.info(
                    "Rebuilt %d procedure(s) for session %s (attempt %d)",
                    len(stored_ids), cc_session_id, attempts + 1,
                )
        except (ProcedureBuilderUnavailable, TimeoutError):
            # Providers still down — keep it pending for a later cycle. No
            # db.rollback(): shared SerializedConnection (a rollback would discard
            # other coroutines' pending writes); procedure stores self-commit so
            # there is nothing of ours to undo.
            await queue.reset_to_pending(item_id)
        except Exception:
            # Deterministic failure (bad transcript / parse) — don't loop forever.
            logger.warning(
                "Procedure rebuild failed permanently for session %s",
                cc_session_id, exc_info=True,
            )
            await queue.mark_discarded(item_id, "rebuild raised non-retryable error")


async def _exhaust_procedure_rebuild(db: aiosqlite.Connection, queue, item: dict) -> None:
    """Give up on a rebuild after the attempt cap and record the loss honestly."""
    import uuid

    from genesis.db.crud import observations as obs_crud

    item_id = item["id"]
    try:
        payload = json.loads(item.get("payload_json", "{}"))
    except (json.JSONDecodeError, TypeError):
        payload = {}
    cc_session_id = payload.get("cc_session_id") or payload.get("session_id") or "?"

    await queue.mark_discarded(
        item_id,
        f"procedure rebuild exhausted {_MAX_PROCEDURE_REBUILD_ATTEMPTS} attempts",
    )
    with contextlib.suppress(Exception):
        await obs_crud.create(
            db,
            id=str(uuid.uuid4()),
            source="procedure_rebuild",
            type="procedure_extraction_lost",
            content=(
                f"Procedure extraction permanently lost for session {cc_session_id}: "
                f"the builder stayed provider-exhausted across "
                f"{_MAX_PROCEDURE_REBUILD_ATTEMPTS} rebuild attempts. A reusable "
                f"playbook that should have been learned was not."
            ),
            priority="medium",
            created_at=datetime.now(UTC).isoformat(),
            skip_if_duplicate=True,
        )
    logger.warning(
        "Procedure rebuild EXHAUSTED for session %s after %d attempts",
        cc_session_id, _MAX_PROCEDURE_REBUILD_ATTEMPTS,
    )


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
    from genesis.db.crud import cc_sessions as sessions_crud

    # Phase 1: Auto-register untracked transcripts from filesystem
    if transcript_dir.is_dir():
        try:
            known_ids = await sessions_crud.get_all_cc_session_ids(db)

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
                await sessions_crud.register_from_filesystem(
                    db,
                    id=session_id,
                    cc_session_id=session_id,
                    started_at=mtime_iso,
                )
        except Exception:
            logger.warning(
                "Filesystem transcript discovery failed — falling back to DB-only",
                exc_info=True,
            )

    # Phase 2: Query all extractable sessions (including newly registered ones)
    return await sessions_crud.get_extractable(
        db, source_tags=_EXTRACTABLE_SOURCE_TAGS,
    )


async def _update_watermark(
    db: aiosqlite.Connection,
    session_id: str,
    line_number: int,
) -> None:
    """Update the extraction watermark for a session."""
    from genesis.db.crud import cc_sessions as sessions_crud

    now_iso = datetime.now(UTC).isoformat()
    await sessions_crud.update_extraction_watermark(
        db, session_id,
        last_extracted_line=line_number,
        last_extracted_at=now_iso,
    )


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
    from genesis.db.crud import cc_sessions as sessions_crud

    # Read existing keywords to merge
    existing_str = await sessions_crud.get_keywords(db, session_id)
    existing = set()
    if existing_str:
        existing = {k.strip() for k in existing_str.split(",") if k.strip()}

    merged = sorted(existing | keywords)
    keywords_str = ", ".join(merged)

    await sessions_crud.update_topic_and_keywords(
        db, session_id, topic=topic, keywords=keywords_str,
    )
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
