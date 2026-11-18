"""The incremental lesson builder (#4813): judge only what follows a session's
progress marker.

Moved out of ``memory/extraction_job.py``, which runs the extraction cycle and
the ``procedure_rebuild`` drain and calls in here for ``segment: incremental``.
The cycle's own helpers (the cap, the queue, the marker read) stay there and
are reached through the module at call time, so a test that patches one of them
patches what runs here.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import aiosqlite

from genesis.learning.procedural.judge import ProcedureBuilderUnavailable
from genesis.memory import extraction_job as ej

if TYPE_CHECKING:
    from genesis.routing.router import Router

logger = logging.getLogger(__name__)


# Parts one incremental run judges per session (each part is at most one builder
# call): a long backlog catches up over a few cycles, and the per-session cap
# still bounds the lessons it can add.
_PARTS_PER_RUN = 4


async def _judge_from_marker(
    db: aiosqlite.Connection,
    session_id: str,
    cc_session_id: str,
    spine: list[dict],
    haystack: str,
    router: Router,
    *,
    marker: str | None,
    transcript_path: Path,
    candidates: bool,
    max_procedures_per_session: int,
    summary: dict,
    counter: str = "struggle_procedures",
) -> str:
    """``segment: incremental`` (#4813): judge the session's unjudged entries.

    Judges up to :data:`_PARTS_PER_RUN` parts from ``marker`` (read before the
    spine was parsed), moving the marker by compare-and-set after each judged
    part or part below the struggle threshold. The timeout wraps each part's
    builder call, never the loop. ``candidates`` (the chunk path flagged messages
    in this request) triggers every part: the flag does not say which part holds
    the candidate, and a part the marker passes is never judged again.

    Returns ``done`` (nothing left), ``pending`` (judged to the end, but an
    unanswered call waits for its result), ``remaining`` (entries left after the last part),
    ``held:cap`` (the cap is spent), ``held:partial-parse`` (a reset whose old id
    is still in the file) or ``refused`` (another writer moved the marker). The builder's errors propagate; parts judged before one keep
    their moved marker.
    """
    from genesis.db.crud import cc_sessions as sessions_crud
    from genesis.learning.procedural.build_segment import select_part
    from genesis.learning.procedural.judge import JUDGE_TIMEOUT_SECS, judge_multi_procedure
    from genesis.learning.procedural.struggle_detector import (
        _MAX_SPINE_CHARS,
        STRUGGLE_THRESHOLD,
        derive_session_origin,
        format_spine_for_judge,
        score_struggle,
    )

    origin = derive_session_origin(spine)  # the whole spine: an early external read marks every part
    for n in range(1, _PARTS_PER_RUN + 1):
        part = select_part(spine, marker, budget_chars=_MAX_SPINE_CHARS)
        if part.reset and await asyncio.to_thread(ej._file_mentions, transcript_path, marker):
            return "held:partial-parse"
        end = "pending" if part.pending_tail else "done"
        if not part.new:
            return end
        reaches_end = part.held_back == 0
        score = score_struggle(part.new)
        triggered = score >= STRUGGLE_THRESHOLD or candidates
        stored = 0
        if triggered:
            cap_left = await ej._lesson_cap_left(db, cc_session_id, max_procedures_per_session)
            if cap_left == 0:
                return "held:cap"
            ids = await asyncio.wait_for(
                judge_multi_procedure(
                    db, part.new, haystack, score, router,
                    source_session_id=cc_session_id, max_new=cap_left,
                    context_text=format_spine_for_judge(part.context),
                    origin_class=origin,
                ),
                timeout=JUDGE_TIMEOUT_SECS,
            )
            stored = len(ids)
            if stored:
                summary[counter] = summary.get(counter, 0) + stored
        moved = (
            part.through_id is not None and part.through_id != marker
            and await sessions_crud.advance_lesson_built_through(
                db, session_id, expected=marker, new=part.through_id,
            )
        )
        logger.info(
            "Lesson segment [incremental] for %s: part=%d entries=%d context=%d score=%.2f "
            "triggered=%s stored=%d held_back=%d pending_tail=%d reset=%s marker=%s",
            session_id, n, len(part.new), len(part.context), score, triggered, stored,
            part.held_back, part.pending_tail, part.reset, "moved" if moved else "not moved",
        )
        if not moved:
            # Nothing to move to (an all-user part) is done when it reached the
            # end; otherwise another writer moved the marker, or it cannot move.
            return end if reaches_end and part.through_id in (None, marker) else "refused"
        marker = part.through_id
        if reaches_end:
            return end
    return "remaining"


async def _incremental_build(
    db: aiosqlite.Connection,
    session_id: str,
    cc_session_id: str,
    spine: list[dict],
    haystack: str,
    router: Router,
    *,
    marker: str | None | object,
    transcript_path: Path,
    candidates: bool,
    max_procedures_per_session: int,
    summary: dict,
) -> None:
    """Stream 1 under ``segment: incremental``: judge from the marker, and queue
    one "continue" rebuild when entries are left (held back, cap spent, or the
    builder failed), so the drain judges them even if the session never gets
    another message. Never raises: the whole-session build does not run in
    this mode, so nothing else would handle the error."""
    trigger = "candidates" if candidates else "score"
    if marker is ej._MARKER_UNREAD:
        # No marker to judge from this cycle: leave it to the drain, so a
        # session that gets no further messages is still judged.
        await ej._enqueue_procedure_rebuild(db, session_id, cc_session_id, trigger=trigger, dedup=True)
        return
    try:
        outcome = await _judge_from_marker(
            db, session_id, cc_session_id, spine, haystack, router,
            marker=marker, transcript_path=transcript_path, candidates=candidates,
            max_procedures_per_session=max_procedures_per_session, summary=summary,
        )
    except (ProcedureBuilderUnavailable, TimeoutError) as exc:
        logger.warning(
            "Procedure builder [incremental] unavailable for session %s (%s) — "
            "queued to continue from the marker", session_id, type(exc).__name__,
        )
        await ej._enqueue_procedure_rebuild(db, session_id, cc_session_id, trigger=trigger, dedup=True)
        return
    except Exception:
        logger.warning(
            "Procedure builder [incremental] failed for session %s — queued to "
            "continue from the marker", session_id, exc_info=True,
        )
        await ej._enqueue_procedure_rebuild(db, session_id, cc_session_id, trigger=trigger, dedup=True)
        return
    # Entries left: more than one run's parts, a spent cap, or another writer's
    # marker (its parse may have been shorter). A ``pending`` call needs nothing
    # here: its result arrives either with a message (this path again) or alone,
    # which the empty-message branch queues for the drain.
    if outcome in ("remaining", "held:cap", "refused"):
        await ej._enqueue_procedure_rebuild(db, session_id, cc_session_id, trigger=trigger, dedup=True)
    elif outcome == "held:partial-parse":
        await _partial_parse_observation(db, cc_session_id)


async def _partial_parse_observation(db: aiosqlite.Connection, cc_session_id: str) -> None:
    """A marker the parse did not reach but the transcript holds: the parse stops
    short of it, so the session is not being judged. Logged and observed (once)."""
    import uuid

    from genesis.db.crud import observations as obs_crud

    logger.warning(
        "Lesson builder holds session %s: its progress marker is in the transcript "
        "but the spine parse did not reach it", cc_session_id,
    )
    with contextlib.suppress(Exception):
        await obs_crud.create(
            db,
            id=str(uuid.uuid4()),
            source="procedure_rebuild",
            type="lesson_builder_parse_stopped_short",
            content=(
                f"The lesson builder is holding session {cc_session_id}: its progress "
                "marker is in the transcript, but the spine parse stops before reaching "
                "it, so the entries after it are not being judged."
            ),
            priority="medium",
            created_at=datetime.now(UTC).isoformat(),
            skip_if_duplicate=True,
        )


async def _released_tail(db: aiosqlite.Connection, session_id: str, spine: list[dict]) -> bool:
    """Whether a judgeable tool entry now follows the marker: a call the last
    part left waiting got its result. False when the marker cannot be read."""
    from genesis.learning.procedural.build_segment import select_part
    from genesis.learning.procedural.struggle_detector import _MAX_SPINE_CHARS

    marker = await ej._read_lesson_marker(db, session_id)
    if marker is ej._MARKER_UNREAD:
        return False
    try:
        part = select_part(spine, marker, budget_chars=_MAX_SPINE_CHARS)
    except Exception:
        logger.warning("Released-tail check failed for session %s", session_id, exc_info=True)
        return False
    return any(e.get("type") == "tool" for e in part.new)


async def _merge_into_pending_rebuild(
    db: aiosqlite.Connection, session_id: str, trigger: str,
) -> bool:
    """Fold a new request into a pending ``procedure_rebuild`` item for the
    session, if there is one; True when one was found.

    A ``candidates`` trigger upgrades the item's trigger: the drain cannot re-run
    the chunk path that flags candidates, so keeping the first writer's ``score``
    would lose a candidates-only build for good.
    """
    from genesis.db.crud import deferred_work as dw_crud

    items = await dw_crud.query_pending(db, work_type=ej._PROCEDURE_REBUILD_WORK, limit=ej._DRAIN_SCAN_LIMIT)
    for item in items:
        read = item.get("payload_json")
        try:
            payload = json.loads(read or "{}")
            if payload.get("session_id") != session_id:
                continue
        except (json.JSONDecodeError, TypeError, AttributeError):
            continue
        if trigger == "candidates":
            payload = {**payload, "trigger": "candidates"}
        # Compare-and-set on what was read: merged only if the item is still
        # pending AND still holds that payload. A drain that claimed it may be
        # reading an older copy, and another writer may have upgraded it (a
        # stale ``score`` write would undo that upgrade), so in either case this
        # request gets an item of its own.
        cursor = await db.execute(
            "UPDATE deferred_work_queue SET payload_json = ?"
            " WHERE id = ? AND status = 'pending' AND payload_json IS ?",
            (json.dumps(payload), item["id"], read),
        )
        await db.commit()
        return cursor.rowcount > 0
    return False


async def _drain_from_marker(
    db: aiosqlite.Connection,
    queue,
    item: dict,
    payload: dict,
    cc_session_id: str,
    transcript_path: Path,
    router: Router,
    *,
    max_procedures_per_session: int,
    summary: dict,
) -> None:
    """One claimed rebuild item under ``segment: incremental`` (#4813): judge the
    session from its progress marker, whatever the item was queued for.

    Completed when the run ends without error (with a fresh item queued when
    entries are left). A builder error, timeout or other error returns it to
    pending with its attempt counted, so 8 failures end in the visible
    ``procedure_extraction_lost`` observation instead of a silent discard.
    """
    from genesis.db.crud import cc_sessions as sessions_crud
    from genesis.db.crud import deferred_work as dw_crud
    from genesis.learning.procedural.struggle_detector import build_spine_and_haystack

    item_id = item["id"]
    session_id = payload.get("session_id") or cc_session_id
    try:
        # The payload as it stands now that the item is claimed: a request
        # merged after the drain listed it (a candidates upgrade) is in this
        # copy only. Read inside the try, so a failed read returns the item.
        current = await dw_crud.get_payload(db, item_id)
        with contextlib.suppress(json.JSONDecodeError, TypeError):
            payload = json.loads(current) if current else payload
        if not await sessions_crud.has_session(db, session_id):
            await queue.mark_discarded(item_id, "session row gone")
            return
        marker = await sessions_crud.get_lesson_built_through(db, session_id)
        spine, haystack = await asyncio.to_thread(build_spine_and_haystack, transcript_path)
        outcome = await _judge_from_marker(
            db, session_id, cc_session_id, spine, haystack, router,
            marker=marker, transcript_path=transcript_path,
            candidates=payload.get("trigger") == "candidates",
            max_procedures_per_session=max_procedures_per_session, summary=summary,
            counter="procedures_rebuilt",
        )
    except (ProcedureBuilderUnavailable, TimeoutError) as exc:
        logger.info(
            "Procedure rebuild [incremental] for session %s: %s on attempt %d — kept pending",
            cc_session_id, type(exc).__name__, item.get("attempts", 0) + 1,
        )
        await queue.reset_to_pending(item_id)
        return
    except Exception:
        logger.warning(
            "Procedure rebuild [incremental] failed for session %s (attempt %d) — "
            "kept pending", cc_session_id, item.get("attempts", 0) + 1, exc_info=True,
        )
        await queue.reset_to_pending(item_id)
        return
    await queue.mark_completed(item_id)
    # ``pending`` is not re-queued, so a call that never gets its result (a
    # crashed session) cannot keep a rebuild cycling. Its result, when it comes,
    # is queued again by the empty-message branch, or judged with a message.
    if outcome in ("remaining", "held:cap", "refused"):
        await ej._enqueue_procedure_rebuild(
            db, session_id, cc_session_id, trigger=payload.get("trigger", "score"), dedup=True,
        )
    elif outcome == "held:partial-parse":
        await _partial_parse_observation(db, cc_session_id)
