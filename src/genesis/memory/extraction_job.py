"""Periodic memory extraction job.

Reads active session transcripts, extracts entities/decisions/relationships,
and stores them in the memory system with provenance.  Runs every 1-2 hours
via the surplus scheduler.

Extraction scope:
- Foreground sessions (user conversations)
- Voice sessions (transcripts written by the voice pipeline)
- Excluded (deliberately — see ``_EXCLUDED_SOURCE_TAGS``): inbox_evaluation
  (external-untrusted fetched-web transcripts — the curated ``.genesis.md``
  output path handles inbox→memory instead), ego/reflection/campaign/etc.
  (their own pipelines).

Every production ``source_tag`` family must be classified as either extractable
or explicitly excluded — the coverage-guard test
(``tests/test_memory/test_extraction_job.py::test_source_tag_coverage_guard``)
enforces this so a new session type can never silently fall out of extraction
(as inbox_evaluation did: the set said "inbox" but the monitor tags
"inbox_evaluation", so 320 sessions matched nothing for months).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import aiosqlite

from genesis.env import cc_project_dir, voice_transcript_dir
from genesis.learning.procedural.judge import ProcedureBuilderUnavailable
from genesis.learning.procedural.lesson_builder_config import (
    CAP_WINDOW as _LESSON_CAP_WINDOW,
)
from genesis.learning.procedural.lesson_builder_config import (
    builder_mode as lesson_builder_mode,
)
from genesis.learning.procedural.lesson_builder_config import segment_mode
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
    read_transcript_delta,
)

if TYPE_CHECKING:
    from genesis.memory.linker import MemoryLinker
    from genesis.memory.store import MemoryStore
    from genesis.routing.router import Router

logger = logging.getLogger(__name__)

# Session types eligible for extraction. 'voice' rows are written by the
# voice transcript writer (channels/voice/transcript_writer.py) — their
# transcripts live in voice_transcript_dir(), not the CC projects dir.
# NOTE: "inbox" was a phantom tag here for months — the inbox monitor tags its
# sessions "inbox_evaluation" (never "inbox"), so this set matched ZERO inbox
# rows. inbox_evaluation is now a DELIBERATE exclusion (see below), not a bug.
_EXTRACTABLE_SOURCE_TAGS = {"foreground", "voice"}

# Session types deliberately NOT transcript-extracted, each with its reason.
# Documentation + the coverage-guard's source of truth; NOT consumed by the
# extraction logic (which reads only _EXTRACTABLE_SOURCE_TAGS). Keeping the full
# taxonomy here makes "should this new tag be extracted?" a conscious decision
# instead of a silent hole.
_EXCLUDED_SOURCE_TAGS = frozenset({
    "inbox_evaluation",  # external-untrusted fetched-web transcript; the curated
                         # .genesis.md output path handles inbox→memory instead
    "genesis_ego_cycle",  # ego has its own persistence
    "user_ego_cycle",  # ego has its own persistence
    "ego_cycle",  # legacy ego tag
    "ego_dispatch",  # ego-dispatched work; own pipeline
    "ego_proposal_executor",  # ego proposal execution; own pipeline
    "campaign",  # campaign sessions; own artifacts
    "sentinel",  # health/incident monitor; not user knowledge
    "weekly_assessment",  # internal assessment; own outputs
    "quality_calibration",  # eval/calibration; not user knowledge
    "direct_session",  # direct MCP session runner; own handling
    "mail_follow_up",  # mail pipeline; own handling
    "mail_reply",  # mail pipeline; own handling
    "models_md_synthesis",  # models.md synthesis job; own output
    "agent_api",  # peer-agent receipts: external-untrusted, like inbox_evaluation;
                  # their scanned result artifact is the owned output
})

# Prefix families whose per-run tags are all excluded (e.g. "reflection_deep",
# "reflection_light", "reflection_strategic"; "user_job:<uuid>"). NOTE: a
# source_tag written via raw parameterized SQL (positional bind) rather than a
# `source_tag="..."` keyword literal is invisible to the coverage-guard's regex
# scan, so it MUST be registered here by hand — e.g. "priority_<n>" LEGACY
# rows (their raw-SQL writer in resilience/cc_budget.py was deleted
# 2026-09-04, but existing installs still carry the rows).
_EXCLUDED_SOURCE_TAG_PREFIXES = ("reflection", "user_job", "priority")

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
# Rebuilds the drain runs per extraction cycle (each one is a builder model call).
_DRAIN_BATCH = 20
# Pending rebuilds the drain reads to find a batch's worth of eligible ones. A
# resource bound only (one live install's deferred_work_queue held 44
# procedure_rebuild rows in total, all completed, on 2026-11-15), and hitting it
# is logged.
_DRAIN_SCAN_LIMIT = 1000


async def _lesson_cap_left(
    db: aiosqlite.Connection, cc_session_id: str, cap: int, *, shadow: bool = False,
) -> int:
    """New lessons this source session may still add in the rolling window.

    Counts ``procedural_memory`` rows created in the window by the session:
    ``created_session_id``, set once at insert, so a refine that rewrites
    ``source`` cannot reopen a slot. A row written before that column existed
    falls back to the session its JSON ``source`` names; a ``source`` that is not
    valid JSON is not counted (the CASE keeps ``json_extract``, which would
    raise, off it).
    ``shadow`` also counts the lessons shadow mode recorded for the session, so
    shadow makes the same number of builder calls live would.
    """
    since = datetime.now(UTC) - _LESSON_CAP_WINDOW
    cursor = await db.execute(
        "SELECT COUNT(*) FROM procedural_memory"
        " WHERE COALESCE(created_session_id, CASE WHEN json_valid(source)"
        " THEN json_extract(source, '$.session_id') END) = ?"
        " AND datetime(created_at) >= datetime(?)",
        (cc_session_id, since.isoformat()),
    )
    row = await cursor.fetchone()
    used = row[0] if row else 0
    if shadow:
        from genesis.learning.procedural.lesson_builder_config import shadow_count

        used += await asyncio.to_thread(shadow_count, cc_session_id, since)
    return max(0, cap - used)


async def _segment_shadow(
    db: aiosqlite.Connection,
    session_id: str,
    cc_session_id: str,
    spine: list[dict],
    *,
    segment: str,
    marker: str | None | object,
    transcript_path: Path,
    candidates: bool,
    lever: str,
    cap_left: int | None,
    max_procedures_per_session: int,
    whole_score: float,
    stored: int,
    path: str = "cycle",
) -> None:
    """``segment: shadow`` (#4807): say what judging only the unjudged part would
    do, and move the progress marker as that rule would move it.

    Called after the whole-session build succeeded (or decided not to build), so
    a failed build holds the marker. ``marker`` is the value read before the
    spine was parsed, and the compare-and-set expects it, so a cycle working
    from an older parse can never move the marker past a newer cycle's write.
    The marker holds when the lever is ``off`` (nothing was judged), when the
    part would trigger a build but the cap is spent, and on a reset whose old id
    is still in the transcript file (the parse stopped short; a real reset means
    the file was replaced). Otherwise it moves to the part's last tool entry.
    Observation only: it changes nothing the builder stores, and an error here
    is logged and never fails the cycle.

    ``whole_score`` and ``stored`` (lessons the whole build kept this cycle, 0
    when it did not run) go on the line, so each line can be compared with what
    the whole build did without joining other lines. ``path`` says which
    judged it: ``cycle`` (Stream 1) or ``drain`` (a queued rebuild, which builds
    whatever the trigger, so its would_trigger is not a saved call).
    """
    from genesis.learning.procedural.build_segment import select_part
    from genesis.learning.procedural.struggle_detector import (
        _MAX_SPINE_CHARS,
        STRUGGLE_THRESHOLD,
        format_spine_for_judge,
        score_struggle,
    )

    try:
        if segment != "shadow" or marker is _MARKER_UNREAD:
            return
        from genesis.db.crud import cc_sessions as sessions_crud

        part = select_part(spine, marker, budget_chars=_MAX_SPINE_CHARS)
        score = score_struggle(part.new)
        would_trigger = score >= STRUGGLE_THRESHOLD or candidates
        if lever == "off":
            held = "off"
        elif part.reset and await asyncio.to_thread(_file_mentions, transcript_path, marker):
            held = "partial-parse"
        elif would_trigger and (
            cap_left if cap_left is not None
            else await _lesson_cap_left(
                db, cc_session_id, max_procedures_per_session, shadow=lever == "shadow",
            )
        ) == 0:
            held = "cap"
        else:
            held = ""
        moved = False
        if not held and part.through_id is not None and part.through_id != marker:
            moved = await sessions_crud.advance_lesson_built_through(
                db, session_id, expected=marker, new=part.through_id,
            )
            if not moved:
                held = "changed-elsewhere"
        logger.info(
            "Lesson segment [shadow] for %s: entries=%d chars=%d context=%d "
            "context_chars=%d score=%.2f would_trigger=%s whole_score=%.2f "
            "stored=%d reset=%s pending_tail=%d held_back=%d lever=%s path=%s marker=%s",
            session_id, len(part.new), len(format_spine_for_judge(part.new)),
            len(part.context), len(format_spine_for_judge(part.context)), score,
            would_trigger, whole_score, stored, part.reset, part.pending_tail,
            part.held_back, lever, path,
            "moved" if moved else f"held:{held}" if held else "unchanged",
        )
    except Exception:
        logger.warning("Lesson segment shadow failed for session %s", session_id, exc_info=True)


def _incremental():
    """The incremental lesson builder's module, imported on use: it imports this
    module, so a top-level import here would be circular."""
    from genesis.learning.procedural import incremental

    return incremental


# A marker read that failed: the shadow step then does nothing this cycle.
_MARKER_UNREAD = object()


async def _read_lesson_marker(db: aiosqlite.Connection, session_id: str) -> str | None | object:
    """The stored progress marker, or :data:`_MARKER_UNREAD` if it cannot be read."""
    from genesis.db.crud import cc_sessions as sessions_crud

    try:
        return await sessions_crud.get_lesson_built_through(db, session_id)
    except Exception:
        logger.warning("Lesson marker read failed for session %s", session_id, exc_info=True)
        return _MARKER_UNREAD


def _file_mentions(path: Path, needle: object) -> bool:
    """Whether ``needle`` occurs in the file's bytes. A marker id that the
    parse did not find but the file still holds means the parse stopped short,
    not that the file was replaced. Unreadable counts as mentioned (hold)."""
    if not isinstance(needle, str) or not needle:
        return False
    target = needle.encode()
    try:
        with open(path, "rb") as f:
            tail = b""
            while chunk := f.read(1 << 20):
                if target in tail + chunk:
                    return True
                tail = chunk[-len(target):]
    except OSError:
        return True
    return False


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


# Public alias for reuse by sibling extractors (e.g. inbox.eval_memory) so
# they don't import the private _check_claim_duplicate across modules.
check_claim_duplicate = _check_claim_duplicate


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
        # Incremental resume: seek to the stored byte offset (= start of line
        # last_line) and read only the delta. Disabled for reference-only
        # mining and start_line_override re-reads, which deliberately re-scan.
        use_incremental = not reference_only_mode and start_line_override is None
        start_byte = session.get("last_extracted_byte") if use_incremental else None

        # Filesystem discovery + transcript read run OFF the event loop
        # (asyncio.to_thread): synchronous transcript I/O here was blocking the
        # loop for 6-7s per cycle and starving memory-recall (503s). The
        # incremental read above keeps the offloaded work tiny; the offload is
        # defense-in-depth for a legitimately-large delta. See follow-up 470cec53.
        transcript_path = await asyncio.to_thread(
            _find_transcript, transcript_dir, cc_session_id
        )
        if not transcript_path and session.get("source_tag") == "voice":
            # Voice sessions keep their transcripts outside the CC projects
            # dir (same CC-JSONL format, written by the voice transcript
            # writer) so the CC resume picker never lists them.
            transcript_path = await asyncio.to_thread(
                _find_transcript, voice_transcript_dir(), cc_session_id
            )

        if not transcript_path:
            continue

        # Read new messages since last extraction (incremental + off-loop).
        delta = await asyncio.to_thread(
            read_transcript_delta,
            transcript_path,
            start_line=last_line,
            start_byte=start_byte,
        )
        if delta.failed:
            # Transcript I/O error (stat/read). Discard any partial read and
            # retry cleanly next cycle. NEVER advance or initialize a watermark
            # on a failed read — empty OR partial: a partial message set committed
            # here (readline() raising mid-stream after some lines parsed) would
            # advance past still-unread content on a transient glitch and drop it.
            continue
        messages = delta.messages
        if not messages:
            # No new user/assistant messages. If the file changed (non-message
            # lines appended), advance BOTH watermarks so the next cycle
            # stat-gates instead of re-reading from byte 0. Skip in
            # reference-only mode (leaves prod extraction state untouched) and
            # when the stat-gate already confirmed nothing changed.
            if use_incremental and not delta.unchanged:
                await _update_watermark(
                    db, session_id, delta.new_line_count,
                    last_extracted_byte=delta.new_byte_offset,
                )
                # The appended lines may hold only a tool result (the delta
                # reader drops those), so record outcomes here too: no later
                # cycle re-reads this transcript unless new text arrives.
                spine = await _record_tool_outcomes_from(db, transcript_path, cc_session_id)
                # Under incremental the lines may be the result of a call the
                # last part left waiting; no message cycle will bring the
                # session back for it, so the drain judges it from the marker.
                # Only then: a hook or system line alone queues nothing.
                if (
                    spine
                    and segment_mode() == "incremental"
                    and lesson_builder_mode() == "live"
                    and await _incremental()._released_tail(db, session_id, spine)
                ):
                    await _enqueue_procedure_rebuild(
                        db, session_id, cc_session_id, trigger="score", dedup=True,
                    )
            continue

        chunks = chunk_messages(messages, chunk_size=chunk_size)
        # On a truncation/rotation reset the delta re-emits absolute line numbers
        # from 0, so the line base must reset too — otherwise max() below pins
        # max_line to the stale pre-truncation watermark while last_chunk_end_byte
        # points into the shrunk file, breaking the byte==line invariant. The
        # empty-branch above already resets by writing delta.new_line_count.
        max_line = 0 if delta.truncated_reset else last_line
        # Byte offset of the start of line `max_line` — the incremental resume
        # point, kept in lock-step with the line watermark below. None until the
        # chunk loop sets it (guaranteed: this path only runs with ≥1 message).
        last_chunk_end_byte: int | None = None
        all_keywords: set[str] = set()
        latest_topic = ""
        session_extraction_count = 0
        session_candidates: list[dict] = []  # flag-only chunk signal (C2b)

        for chunk in chunks:
            chunk_start = chunk[0].line_number
            chunk_end = chunk[-1].line_number
            max_line = max(max_line, chunk_end + 1)
            # end_byte of the last message = byte start of line (chunk_end + 1)
            # = byte start of line `max_line`. Kept in lock-step so the resume
            # point survives a cap-break (the last processed chunk sets both).
            last_chunk_end_byte = chunk[-1].end_byte

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

                    # Entity layer (E3): give extracted entities and
                    # relationships IDENTITY (entities/entity_mentions/
                    # entity_links). Parallel to memory_links, never a
                    # replacement; fail-open per memory.
                    if extraction.entities or extraction.relationships:
                        try:
                            from genesis.memory.entity_registry import (
                                record_extraction,
                            )

                            entity_counts = await record_extraction(
                                db, memory_id, extraction,
                            )
                            summary.setdefault("entity_mentions", 0)
                            summary.setdefault("entity_links", 0)
                            summary["entity_mentions"] += entity_counts["mentions"]
                            summary["entity_links"] += entity_counts["links"]
                        except Exception:
                            logger.warning(
                                "Entity-layer recording failed for %s",
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
            await _update_watermark(
                db, session_id, max_line,
                last_extracted_byte=last_chunk_end_byte,
            )
            if all_keywords or latest_topic:
                await _update_session_index(
                    db, session_id,
                    keywords=all_keywords, topic=latest_topic,
                )

            shadow = False  # set from the lever below; read by the except
            # Stream 1: the whole-session procedure BUILDER (runs once per
            # session, after all chunks). Parses the full JSONL into a user-blind
            # action spine + a grounding haystack, scores struggle, and runs the
            # multi-procedure builder when EITHER the struggle score crosses the
            # threshold OR the chunk path flagged candidates. Returns 0..N
            # topic-segmented playbooks.
            try:
                from genesis.learning.procedural.struggle_detector import (
                    STRUGGLE_THRESHOLD,
                    build_spine_and_haystack,
                    score_struggle,
                )

                # Offload the synchronous transcript read+parse to a thread —
                # `build_spine_and_haystack` iterates a whole JSONL file
                # (`for line in f`), which wedged the event loop ~1s at a time
                # from this background extraction job (recall 503s correlated).
                # The progress marker as it stood BEFORE this parse: compared with
                # the spine this cycle parses, and the value its compare-and-set
                # expects, so a cycle working from an older parse cannot move it
                # backwards past a newer cycle's write.
                marker_before = await _read_lesson_marker(db, session_id)
                spine, haystack = await asyncio.to_thread(
                    build_spine_and_haystack, transcript_path
                )
                await _record_tool_outcomes(db, spine, cc_session_id)
                struggle_score = score_struggle(spine)
                struggle_triggered = struggle_score >= STRUGGLE_THRESHOLD
                # Per-session cap on NEW draft lessons, counted from the store
                # over a rolling window. A local counter restarted at 0 every
                # cycle, so one long session reached 811 lessons.
                # Shadow runs the real builder and records what it would store.
                lever = lesson_builder_mode()
                # Read once per session per cycle, so a flip mid-cycle cannot run
                # both paths on one session. Incremental needs the builder live;
                # otherwise it runs as shadow (#4813).
                segment = segment_mode()
                if segment == "incremental" and lever != "live":
                    segment = "shadow"
                if segment == "incremental":
                    await _incremental()._incremental_build(
                        db, session_id, cc_session_id, spine, haystack, router,
                        marker=marker_before, transcript_path=transcript_path,
                        candidates=bool(session_candidates),
                        max_procedures_per_session=max_procedures_per_session,
                        summary=summary,
                    )
                builder_mode = (
                    lever
                    if (struggle_triggered or session_candidates) and segment != "incremental"
                    else "off"
                )
                shadow = builder_mode == "shadow"
                cap_left = 0
                if builder_mode != "off":
                    cap_left = await _lesson_cap_left(
                        db, cc_session_id, max_procedures_per_session, shadow=shadow,
                    )
                    if cap_left == 0:
                        logger.info(
                            "Procedure builder skipped for %s: %d lessons already "
                            "built from it in the last 24h",
                            session_id, max_procedures_per_session,
                        )
                if cap_left:
                    from genesis.learning.procedural.judge import (
                        JUDGE_TIMEOUT_SECS,
                        judge_multi_procedure,
                    )

                    stored_ids = await asyncio.wait_for(
                        judge_multi_procedure(
                            db, spine, haystack, struggle_score, router,
                            source_session_id=cc_session_id,
                            max_new=cap_left,
                            shadow=shadow,
                        ),
                        timeout=JUDGE_TIMEOUT_SECS,
                    )
                    if shadow:
                        summary["shadow_lessons"] = (
                            summary.get("shadow_lessons", 0) + len(stored_ids)
                        )
                    elif stored_ids:
                        summary["struggle_procedures"] = (
                            summary.get("struggle_procedures", 0) + len(stored_ids)
                        )
                    # Measure SLM over-flagging: the builder fired on chunk
                    # candidates alone, with no struggle signal.
                    if session_candidates and not struggle_triggered:
                        logger.info(
                            "Procedure builder (%s) fired on candidates-only for %s "
                            "(%d candidates, score=%.2f): stored=%d",
                            builder_mode, session_id, len(session_candidates),
                            struggle_score, len(stored_ids),
                        )
                    logger.info(
                        "Procedure builder (%s) for %s: score=%.2f, candidates=%d, "
                        "stored=%d",
                        builder_mode, session_id, struggle_score,
                        len(session_candidates), len(stored_ids),
                    )
                # Reached only when the whole build above did not raise, so a
                # failed build never moves the progress marker.
                await _segment_shadow(
                    db, session_id, cc_session_id, spine,
                    segment=segment,
                    marker=marker_before, transcript_path=transcript_path,
                    candidates=bool(session_candidates), lever=lever,
                    cap_left=cap_left if builder_mode != "off" else None,
                    max_procedures_per_session=max_procedures_per_session,
                    whole_score=struggle_score, stored=len(stored_ids) if cap_left else 0,
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
                if shadow:
                    # A queued rebuild runs only in live and stores, so queuing a
                    # shadow build would store what shadow promised not to.
                    logger.warning(
                        "Procedure builder [shadow] unavailable for session %s (%s) — "
                        "not queued", session_id, type(exc).__name__,
                    )
                else:
                    logger.warning(
                        "Procedure builder unavailable for session %s (%s) — "
                        "re-queuing for rebuild", session_id, type(exc).__name__,
                    )
                    await _enqueue_procedure_rebuild(
                        db, session_id, cc_session_id,
                        trigger="candidates" if session_candidates else "score",
                    )
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


async def _record_tool_outcomes_from(
    db: aiosqlite.Connection, transcript_path: Path, cc_session_id: str
) -> list[dict] | None:
    """Parse ``transcript_path`` and record its outcomes; never raises.

    For a cycle whose delta has no message, which skips the builder's parse.
    Returns the spine, or None when the parse failed.
    """
    try:
        from genesis.learning.procedural.struggle_detector import build_spine_and_haystack

        spine, _ = await asyncio.to_thread(build_spine_and_haystack, transcript_path)
    except Exception:
        logger.warning(
            "Tool-outcome parse failed for session %s — extraction unaffected",
            cc_session_id, exc_info=True,
        )
        return None
    await _record_tool_outcomes(db, spine, cc_session_id)
    return spine


async def _record_tool_outcomes(
    db: aiosqlite.Connection, spine: list[dict], cc_session_id: str
) -> None:
    """Record every main-session tool call's outcome from the parsed spine (#4712).

    Best-effort and isolated: a failure here is logged and never affects
    extraction or the procedure builder. Idempotent (unique ``tool_use_id``), so
    a later cycle re-reading the same transcript writes nothing twice, and it is
    what records a call whose result had not arrived on an earlier cycle.
    """
    try:
        from genesis.db.crud.tool_call_outcomes import insert_outcomes
        from genesis.learning.tool_outcomes import outcome_rows

        rows = await asyncio.to_thread(
            outcome_rows, spine, fallback_session_id=cc_session_id
        )
        await insert_outcomes(db, rows)
    except Exception:
        logger.warning(
            "Tool-outcome recording failed for session %s — extraction unaffected",
            cc_session_id, exc_info=True,
        )


async def _enqueue_procedure_rebuild(
    db: aiosqlite.Connection, session_id: str, cc_session_id: str,
    *, trigger: str = "score", dedup: bool = False,
) -> None:
    """Re-queue a session whose procedure build died on provider exhaustion.

    Durable (deferred_work_queue): a later extraction cycle drains it and rebuilds
    the procedures from the full transcript. Best-effort — a failure to enqueue
    must not break the extraction cycle. Rebuild idempotency is guaranteed by the
    novelty/dedup gate in ``store_procedure_checked``.

    The drain reads an item by the current ``segment`` (#4813): under
    incremental it judges from the session's progress marker, otherwise it
    rebuilds the whole session. ``trigger`` (``score`` or ``candidates``) says
    what made the build run, since the drain cannot re-run the chunk path that
    flags candidates. ``dedup`` skips the enqueue when a pending item already
    names the session (incremental items only say "continue from the marker").
    """
    from genesis.resilience.deferred_work import (
        DRAIN,
        MEMORY_OPS,
        DeferredWorkQueue,
    )

    try:
        if dedup and await _incremental()._merge_into_pending_rebuild(db, session_id, trigger):
            return
        queue = DeferredWorkQueue(db)
        await queue.enqueue(
            work_type=_PROCEDURE_REBUILD_WORK,
            call_site_id="38_procedure_extraction",
            priority=MEMORY_OPS,
            payload=json.dumps(
                {"session_id": session_id, "cc_session_id": cc_session_id, "trigger": trigger}
            ),
            reason="provider exhausted — auto-rebuild queued (self-healing; drains next extraction cycle)",
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

    # Off or shadow: queued rebuilds stay pending, with no attempt spent, until
    # the builder is live again.
    if lesson_builder_mode() != "live":
        return

    queue = DeferredWorkQueue(db)
    # Read past the batch: an item whose session is at its cap is skipped without
    # moving, so a page of exactly the batch size could hold only capped items
    # and starve every eligible one behind them until a cap window expired.
    items = await dw_crud.query_pending(
        db, work_type=_PROCEDURE_REBUILD_WORK, limit=_DRAIN_SCAN_LIMIT,
    )
    if len(items) >= _DRAIN_SCAN_LIMIT:
        logger.warning(
            "Procedure rebuild drain read %d pending items, its scan limit; later "
            "items wait for a later cycle", len(items),
        )
    capped: set[str] = set()  # sessions found at their cap during this drain
    processed = 0
    segment = segment_mode()  # read once per drain
    incremental = segment == "incremental"  # the drain runs only when live
    for item in items:
        if processed >= _DRAIN_BATCH:
            break
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

        # The session's cap is spent for now: leave the item pending, with no
        # attempt spent, so a later cycle rebuilds it once the window moves on.
        # Checked before the transcript search, which touches the filesystem.
        if cc_session_id in capped:
            continue
        cap_left = (
            await _lesson_cap_left(db, cc_session_id, max_procedures_per_session)
            if cc_session_id else 0
        )
        if cc_session_id and cap_left == 0:
            capped.add(cc_session_id)
            continue

        transcript_path = (
            _find_transcript(transcript_dir, cc_session_id)
            or _find_transcript(voice_transcript_dir(), cc_session_id)
            if cc_session_id else None
        )
        if not transcript_path:
            # Transcript rotated/gone — nothing to rebuild from.
            await queue.mark_discarded(item_id, "transcript not found")
            continue

        # Claim atomically: a second process running a cycle (the backfill
        # script beside the server) may have taken this item already. A lost
        # claim uses no batch slot.
        if not await dw_crud.claim_pending(
            db, item_id, last_attempt_at=datetime.now(UTC).isoformat(),
        ):
            continue
        processed += 1
        if incremental:
            await _incremental()._drain_from_marker(
                db, queue, item, payload, cc_session_id, transcript_path, router,
                max_procedures_per_session=max_procedures_per_session, summary=summary,
            )
            continue
        # The cc_sessions row id, for the progress marker; an item queued
        # without one (none are today) only skips the shadow step.
        row_id = payload.get("session_id")
        try:
            marker_before = (
                await _read_lesson_marker(db, row_id) if row_id else _MARKER_UNREAD
            )
            # Offload the synchronous transcript read+parse off the event loop
            # (see the run_extraction_cycle call site) — a whole-file JSONL
            # parse must not wedge the loop from this background job.
            spine, haystack = await asyncio.to_thread(
                build_spine_and_haystack, transcript_path
            )
            score = score_struggle(spine)
            stored_ids = await asyncio.wait_for(
                judge_multi_procedure(
                    db, spine, haystack, score, router,
                    source_session_id=cc_session_id,
                    max_new=cap_left,
                ),
                timeout=JUDGE_TIMEOUT_SECS,
            )
            await queue.mark_completed(item_id)
            summary["procedures_rebuilt"] = (
                summary.get("procedures_rebuilt", 0) + len(stored_ids)
            )
            # The rebuild judged the session, as Stream 1's build does, so the
            # marker moves as it would there. Without this a session with no new
            # messages keeps a stale marker, and its next shadow line counts
            # entries this rebuild already judged. After mark_completed, so the
            # item's fate never depends on this observation step. The lever is
            # live: the drain returns early otherwise (see its first check).
            await _segment_shadow(
                db, row_id, cc_session_id, spine,
                segment=segment, marker=marker_before, transcript_path=transcript_path,
                candidates=False, lever="live", cap_left=cap_left,
                max_procedures_per_session=max_procedures_per_session,
                whole_score=score, stored=len(stored_ids), path="drain",
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
                f"the rebuild failed (provider exhaustion, a timeout or an error) on "
                f"all {_MAX_PROCEDURE_REBUILD_ATTEMPTS} attempts. A reusable "
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


# Adoption liveness window: a transcript written within this window is a
# live session (the heartbeats table uses the same 10-minute staleness
# convention). Beyond it, only a fresh heartbeat proves life.
_ADOPT_ALIVE_WINDOW = timedelta(minutes=10)


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
                # Honest adoption status (2026-09-04: the old hardcoded
                # 'completed' recorded LIVE sessions as finished — a
                # 4-second-old session read as done). ALIVE evidence =
                # a transcript still being written (fresh mtime), or a
                # fresh heartbeat for this cc_session_id. Dead adoptions
                # carry completed_at = mtime, the best-known end time.
                alive = (datetime.now(UTC) - mtime) <= _ADOPT_ALIVE_WINDOW
                if not alive:
                    try:
                        from genesis.db.crud import session_heartbeats

                        hb = await session_heartbeats.get_active(db)
                        alive = any(
                            h.get("cc_session_id") == session_id for h in hb
                        )
                    except Exception:
                        alive = False  # no heartbeat plane → mtime verdict stands
                await sessions_crud.register_from_filesystem(
                    db,
                    id=session_id,
                    cc_session_id=session_id,
                    started_at=mtime_iso,
                    status="active" if alive else "completed",
                    completed_at=None if alive else mtime_iso,
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
    *,
    last_extracted_byte: int | None = None,
) -> None:
    """Update the extraction watermark for a session.

    ``last_extracted_byte`` is the incremental-resume hint (byte offset of the
    start of line ``line_number``); None leaves the stored byte offset untouched.
    """
    from genesis.db.crud import cc_sessions as sessions_crud

    now_iso = datetime.now(UTC).isoformat()
    await sessions_crud.update_extraction_watermark(
        db, session_id,
        last_extracted_line=line_number,
        last_extracted_at=now_iso,
        last_extracted_byte=last_extracted_byte,
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
