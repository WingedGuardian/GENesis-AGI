"""Detached ledger shadow worker (session-manager PR-3) — the run loop.

Spawned by the PreCompact hook after each compaction snapshot (fire-and-
forget; zero impact on the hook's 5s budget). Reads the transcript delta
since its own cursor, asks headless Haiku for missed agreements/pivots,
matches them against the live ledger, and records SHADOW rows — never a
``session_ledger`` write, never anything user-visible.

Discipline (WS-C worker lineage):

- Own short-lived DB connection; the server's SerializedConnection is
  never touched. All failures are recorded, never raised — nothing is
  attached to read a detached process's exit status.
- Worker-owned cursor (``ledger_shadow_cursor.json``): advanced ONLY
  after shadow rows commit and only for ok/empty_delta outcomes, so a
  crashed/failed/pre-migration run self-heals by re-covering its byte
  range at the next compaction (``duplicate_of`` matching absorbs the
  re-covered proposals).
- Per-session flock (NOT the WS-C theme-worker slots — different cadence
  and budget); the loser records ``lock_busy`` and exits, cursor-safe.
- ``--backfill`` (commit 8) replays historical windows with
  ``trigger='backfill'`` and never touches the cursor.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import tempfile
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from genesis.db.crud import session_ledger_shadow as shadow_crud
from genesis.db.crud.session_charters import ledger_list
from genesis.env import genesis_db_path
from genesis.session_awareness.headless import run_headless_json
from genesis.session_awareness.ledger_extractor import (
    ASSISTANT_SNIPPET_CHARS,
    EXTRACTOR_MODEL,
    EXTRACTOR_TIMEOUT_S,
    PROMPT_VERSION,
    USER_TURN_CHARS,
    build_prompt,
    match_proposals,
    parse_verdict,
)
from genesis.session_awareness.ledger_shadow_config import effective_mode
from genesis.session_awareness.transcript import parse_delta
from genesis.session_charter import _SAFE_SESSION_ID as _SESSION_ID_RE

# The spawner captures this process's stderr to the worker error log, so a
# warning here is durable rather than discarded.
logger = logging.getLogger(__name__)

CURSOR_FILENAME = "ledger_shadow_cursor.json"
LOCK_FILENAME = "ledger_shadow.lock"

# The session id becomes ONE filesystem path component under
# ``~/.genesis/sessions/`` (cursor + lock live there). A traversal or
# absolute-path value would place worker state outside the sessions root, so
# an unsafe id skips the run entirely (fail toward doing nothing, never toward
# writing elsewhere).
#
# IMPORTED, not re-declared (see the import block): a local copy here
# diverged from the sibling it claimed to mirror the first time it was
# written. session_charter owns the src/ definition, next to the
# session_dir() chokepoint that consumes it.

# Defensive ceiling on a single delta read: parse_delta streams line-by-line
# so memory stays flat, but an unbounded window on a monster transcript is
# still wasted work — the prompt keeps only ~24k chars of the NEWEST turns
# anyway, so cap the scanned window to the trailing span.
MAX_WINDOW_BYTES = 64 * 1024 * 1024


def _sessions_root() -> Path:
    """Where per-session state lives.

    Defers to the canonical constant rather than recomputing the path. FOUR
    spellings of this directory existed across the subsystem — the fourth,
    ``scripts/genesis_precompact.py:_SESSIONS_DIR``, is genuinely
    un-shareable, since scripts/ must not import from src/. Two of them being
    equal today is not a guarantee, and the mirror write below depends on the
    two agreeing.
    """
    from genesis.session_charter import SESSIONS_DIR

    return SESSIONS_DIR


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        os.write(fd, json.dumps(data).encode())
    finally:
        os.close(fd)
    os.replace(tmp, str(path))


def _read_cursor(session_dir: Path) -> dict:
    try:
        data = json.loads((session_dir / CURSOR_FILENAME).read_text())
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {"last_byte": 0, "last_run_ts": None, "runs": 0}


def _advance_cursor(session_dir: Path, end_byte: int, prior: dict) -> None:
    """Advance the cursor MONOTONICALLY. Workers serialize on the flock but
    not in spawn order: a later-spawned worker with a higher end-byte can
    finish first, and the earlier worker (its window now consumed) must not
    clobber that progress back down — a regression re-sweeps covered bytes,
    wasting a Haiku call and inflating the precision report's uniques.
    ``prior`` was read under the lock, so max() against it is race-free."""
    _atomic_write_json(
        session_dir / CURSOR_FILENAME,
        {
            "last_byte": max(int(prior.get("last_byte") or 0), end_byte),
            "last_run_ts": _now(),
            "runs": int(prior.get("runs") or 0) + 1,
        },
    )


async def _record_telemetry(db_path: Path | str, status: str, detail: str) -> bool:
    """Best-effort call_site_last_run row for the neural monitor."""
    try:
        from genesis.observability.call_site_recorder import record_last_run_detached

        return await record_last_run_detached(
            str(db_path),
            "ambient_ledger_extractor",
            provider="cc",
            model_id=EXTRACTOR_MODEL,
            response_text=f"status={status}|{detail}"[:200],
            success=status in ("ok", "empty_delta"),
        )
    except Exception:
        return False


# Provenance stamped on every row the extractor promotes. Distinct from
# "ambient" ON PURPOSE: `_default_added_by()` already returns "ambient" for any
# DISPATCHED CC session, and the shadow report's leak invariant keys on this
# value to assert the extractor has written nothing live. One shared value would
# make that check unable to tell the two apart on the day it starts mattering.
# Mirrored by a schema CHECK (the session_ledger_ambient_extractor migration).
PROMOTION_ADDED_BY = "ambient_ledger_extractor"

# INTERIM cap on rows promoted per run. Explicitly temporary: it is a guard held
# until the overseer can prune the extractor's own tier, not a considered
# number. A cap that guesses is worse than a validator that judges — but it
# bounds the blast radius of a bad prompt-version in the meantime. Whatever it
# drops is LOGGED, never silently truncated: a silent cap reads downstream as
# "the extractor found nothing".
PROMOTION_CAP = 5


# The retryable-promotion sweep. Qualification lives in SQL, not in the
# in-memory events of one run, so a proposal whose promotion FAILED (locked
# db, bad row, crash) is simply still here next run — the cursor never has to
# move backwards.
#
# WHERE CORRECTNESS ACTUALLY LIVES. Every clause below is an EFFICIENCY
# filter — it skips work already known to be pointless. The guarantee that an
# agreement is never promoted twice is the novelty recheck inside the write
# transaction (see _promote_live), and only that. This split is deliberate and
# measured: mutation-testing removed `promoted_item_id IS NULL` on its own and
# the idempotency test stayed green, because the recheck caught it. Do not
# re-read these filters as safety properties; adding one that is wrong
# SUPPRESSES agreements silently, which is the failure mode below.
#
# The qualifying bar is a conservative first cut — only unambiguous,
# quote-backed, novel agreements. A false ledger row costs attention in EVERY
# later window, so this starts tight and loosens on evidence:
#   - kind='agreement': a pivot is a change of direction, not a commitment,
#     and reads badly as a checkbox.
#   - quote_verified: the extractor found the words in the transcript, so the
#     row is not a paraphrase of something never said.
#   - match_kind='none': not already a live ledger row at observation time.
#
# `duplicate_of IS NULL` is deliberately ABSENT, and its absence is a fix.
# match_proposals builds its dedup pool from ALL prior events of the session,
# unfiltered by mode or prompt generation. So a re-proposal links to a root
# that the mode/version clauses below then exclude — and the agreement is
# suppressed FOREVER, with no log line and no counter. Letting the chain
# through instead costs one extra recheck per duplicate, which permanently
# marks it. MEASURED on the live corpus: 3/550 events (0.5%) carry a
# duplicate_of, and chain nesting is 0 — so the cost is negligible and the
# suppression it removes is total. The recheck uses the SAME matcher and
# threshold (best_match, >=0.85) as the deduper, against the promoted ledger
# row, so anything duplicate_of would have caught it catches too.
#
# Three deliberate scope guards:
#   - e.mode = 'live': promote only what was proposed UNDER the live promise.
#     Without it, flipping the two keys does not start promoting from now on —
#     it drains the entire backlog gathered while the config promised the live
#     ledger is never written (MEASURED: 550/550 stored events are
#     mode='shadow'). The two-key gate proves CURRENT intent; applying it
#     backwards is more write authority than the operator asked for. A failed
#     promotion's own event was recorded mode='live', so retry is unaffected.
#   - r.prompt_version = current: a prompt-generation bump must not promote a
#     backlog the old prompt produced (the v1 corpus was adjudicated at 43%
#     wanted and is exactly what this excludes).
#   - r.trigger != 'backfill': backfill replays historical windows; those
#     agreements belong to sessions that have already ended.
#
# ORDER BY carries an id tiebreaker because one run stamps a single
# observed_at across its whole batch — without it, which candidates the cap
# takes is implementation-defined and a bug report cannot be reproduced.
_PROMOTION_SWEEP_SQL = """
    SELECT e.* FROM session_ledger_shadow_events e
    JOIN session_ledger_shadow_runs r ON r.run_id = e.run_id
    WHERE e.session_id = ?
      AND e.kind = 'agreement'
      AND e.quote_verified = 1
      AND e.match_kind = 'none'
      AND e.promoted_item_id IS NULL
      AND e.mode = 'live'
      AND r.trigger != 'backfill'
      AND r.prompt_version = ?
    ORDER BY e.observed_at, e.id
"""


async def _promote_live(db_path: Path | str, session_id: str, *, trigger: str) -> dict:
    """Promote qualifying shadow proposals into the REAL ledger. Live mode only.

    Reads its candidates from the shadow store (``_PROMOTION_SWEEP_SQL``), not
    from the calling run's in-memory events — the shadow row IS the retryable
    promotion state, so a failure here costs nothing but time: the event stays
    unpromoted and the next live run sweeps it again. The cursor is therefore
    never coupled to promotion outcome.

    Per candidate, novelty is re-checked INSIDE the write transaction
    (``BEGIN IMMEDIATE`` holds the WAL write lock across recheck + insert), so
    a foreground ``session_ledger_add`` landing between observation-time
    matching and promotion cannot produce a duplicate: the recheck sees it,
    writes the discovered match back onto the event (which permanently
    disqualifies it — ``match_kind`` leaves ``'none'``), and skips.

    Crash windows are closed by ATOMICITY, not by recovery: the ledger insert
    and the event's ``promoted_item_id`` link land in one transaction, so a
    crash leaves either both (promotion complete) or neither (the event still
    qualifies and the next sweep retries it cleanly). The old recovery story —
    re-finding an orphaned row as an exact match of its own text — held only
    while the row stayed open, and silently minted duplicates once it closed.

    The mode gate is re-read per candidate as well as before the sweep: five
    transactions with lock waits between them is a real interval, and the
    documented emergency rollback (``mode: shadow``) must stop the NEXT write,
    not just the next sweep. ``effective_mode`` reads config fresh per call
    (verified — "per call, NO cache"), which is what makes the recheck real.

    Best-effort per row: one bad row must not cost the rest, and none of it
    may cost the shadow record that has already been written.
    """
    if trigger == "backfill":
        # Belt to the sweep's braces: never even open a connection.
        return {"promoted": 0, "skipped_backfill": True}

    import aiosqlite

    from genesis.db.crud import session_charters as crud
    from genesis.session_awareness.ledger_extractor import best_match
    from genesis.session_charter import refresh_mirror

    written = 0
    disqualified = 0
    failed = 0
    n_qualifying = 0
    mode_stopped = False
    sweep_error = False
    try:
        async with aiosqlite.connect(str(db_path), timeout=10) as db:
            # Row factory is REQUIRED, not tidiness: `crud.get` builds
            # `dict(row)`, which raises on a plain tuple. That exception was
            # swallowed by refresh_mirror's best-effort `except`, so the mirror
            # never refreshed and the run reported success anyway. Reproduced
            # end-to-end before this line existed.
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA busy_timeout=5000")
            await crud.upsert_stub(db, session_id)

            cursor = await db.execute(_PROMOTION_SWEEP_SQL, (session_id, PROMPT_VERSION))
            qualifying = [dict(r) for r in await cursor.fetchall()]
            n_qualifying = len(qualifying)
            candidates, dropped = qualifying[:PROMOTION_CAP], qualifying[PROMOTION_CAP:]
            if dropped:
                # Not lost — still unpromoted, swept again next run. Logged so
                # a cap that keeps biting is visible, never silent.
                logger.warning(
                    "ledger promotion cap: taking %d of %d qualifying proposals "
                    "for %s this run, %d deferred to the next sweep (cap=%d)",
                    len(candidates), n_qualifying, session_id, len(dropped), PROMOTION_CAP,
                )

            for ev in candidates:
                if effective_mode() != "live":
                    # Rollback mid-sweep: stop before the NEXT transaction.
                    # Candidates already written stay written (they were made
                    # under a live gate); everything else waits, unpromoted.
                    mode_stopped = True
                    logger.warning(
                        "ledger promotion stopped mid-sweep for %s — mode left "
                        "'live' with %d candidate(s) unprocessed",
                        session_id, len(candidates) - written - disqualified - failed,
                    )
                    break
                try:
                    # Hold the write lock across recheck + insert: in WAL mode
                    # a DEFERRED read snapshot could miss a foreground row
                    # committed after it, and that stale novelty answer is the
                    # TOCTOU this transaction exists to close.
                    await db.execute("BEGIN IMMEDIATE")
                    # OPEN rows only. A CLOSED row is a finished agreement,
                    # and the same agreement being renewed later is a NEW
                    # commitment, not a duplicate of the old one — but matching
                    # against closed rows called it a late match and, because
                    # that verdict is written onto the event, disqualified it
                    # from every future sweep. Permanently: the closed row never
                    # goes away, so the renewal could never be promoted.
                    #
                    # `in_progress` counts as open — that IS a live duplicate.
                    cur = await db.execute(
                        "SELECT id, text FROM session_ledger "
                        "WHERE session_id = ? AND status IN ('open','in_progress')",
                        (session_id,),
                    )
                    rows = await cur.fetchall()
                    kind, matched_id, score = best_match(
                        ev.get("text") or "", [(r["id"], r["text"]) for r in rows]
                    )
                    if kind != "none":
                        # Late match — a human (or an earlier crashed attempt)
                        # already wrote this. Record the truth on the event;
                        # that disqualifies it from every future sweep.
                        await db.execute(
                            "UPDATE session_ledger_shadow_events "
                            "SET match_kind = ?, matched_item_id = ?, match_score = ? "
                            "WHERE id = ?",
                            (kind, matched_id, score, ev["id"]),
                        )
                        await db.commit()
                        disqualified += 1
                        continue
                    item_id = await crud.ledger_add(
                        db,
                        session_id=session_id,
                        text=ev.get("text") or "",
                        source_ref=ev.get("turn_ref"),
                        added_by=PROMOTION_ADDED_BY,
                        # The filter REQUIRES a verified quote and then threw it
                        # away: every promoted row had evidence NULL, which is
                        # both unauditable for a human reading an autonomously
                        # added row AND an automatic failure of the live-mode
                        # leak invariant, which asks each extractor row for its
                        # source text.
                        #
                        # Written to BOTH columns on purpose. `evidence` is what
                        # a human reading the row today sees, and stays useful
                        # until a resolver replaces it with its own attribution.
                        # `source_quote` is the durable provenance no resolver
                        # writes — without it, repo-pulse absorbing this row
                        # erases the quote and the invariant it satisfied
                        # yesterday fails today.
                        evidence=ev.get("quote_preview"),
                        source_quote=ev.get("quote_preview"),
                        # ATOMIC with the link below — one transaction, one
                        # commit. When ledger_add committed on its own, a crash
                        # in the gap left a row no event claimed; the recheck
                        # re-found it only while the row stayed OPEN (exact
                        # match of its own text). Closed before the next sweep,
                        # it was invisible to the open-only recheck and the
                        # still-qualifying event inserted a second copy — while
                        # the leak invariant read the first as unattributed.
                        # Insert + link landing together removes the gap
                        # instead of narrowing it.
                        commit=False,
                    )
                    await db.execute(
                        "UPDATE session_ledger_shadow_events "
                        "SET promoted_item_id = ? WHERE id = ?",
                        (item_id, ev["id"]),
                    )
                    await db.commit()
                    written += 1
                except Exception:
                    failed += 1
                    logger.warning(
                        "ledger promotion failed for one proposal in %s",
                        session_id, exc_info=True,
                    )
                    try:
                        await db.rollback()
                    except Exception:
                        logger.warning("promotion rollback failed", exc_info=True)
            # The mirror is what the NEXT window reads. A promoted row that
            # never reaches charter.md is invisible where it was meant to help,
            # so this is part of the write, not a nicety.
            # Pass the worker's own root explicitly: reading the module
            # constant inside refresh_mirror makes the destination
            # unredirectable, and a test that cannot redirect it writes into
            # the operator's real ~/.genesis/sessions.
            # Only when something actually changed: refreshing on every
            # compaction rewrites charter.md for nothing and widens the window
            # in which this write can interleave with the PreCompact hook's own.
            if written:
                await refresh_mirror(db, session_id, _sessions_root())
    except Exception:
        # Deliberately vague: this wraps the connect AND the whole sweep, so
        # naming one cause would mislead on most of its surface. exc_info
        # carries the specific failure.
        #
        # The flag is the fix for the counters lying by omission: a sweep that
        # died at connect returned the same all-zero dict as one where nothing
        # qualified, so the run reported `ok` — to telemetry AND the detached
        # wrapper — while zero promotion work happened. "Nothing to do" and
        # "could not do anything" are different facts, and only the caller can
        # act on the difference.
        sweep_error = True
        logger.warning("ledger promotion sweep aborted", exc_info=True)

    return {
        "promoted": written,
        "qualifying": n_qualifying,
        "disqualified_at_write": disqualified,
        "failed_rows": failed,
        "sweep_error": sweep_error,
        "mode_stopped": mode_stopped,
    }


async def _record_run(db_path: Path | str, **kwargs) -> bool:
    """One short-lived RW connection: run row + events, single commit.

    Returns False when the write demonstrably did not land (pre-migration
    tables, locked DB) — the caller must then leave the cursor alone.
    """
    import aiosqlite

    try:
        async with aiosqlite.connect(str(db_path), timeout=10) as db:
            await db.execute("PRAGMA busy_timeout=5000")
            return await shadow_crud.record_run(db, **kwargs)
    except Exception:
        return False


async def _load_match_context(
    db_path: Path | str, session_id: str
) -> tuple[list, list, bool]:
    """(live ledger items, prior shadow events, read_ok) for the match stage.

    Best-effort for SHADOW purposes: on failure the extractor still runs and its
    proposals simply carry match_kind='none' (the report recomputes matching
    offline anyway).

    The third value exists because that fail-open posture INVERTS once proposals
    can be promoted. Both novelty signals derive from this read: an empty ledger
    list makes every agreement look unmatched, and empty priors make every one
    look non-duplicate. So a FAILED read does not merely lose information — it
    makes everything look promotable, including exact duplicates of rows the
    user already wrote. Failing open toward MORE write authority is the one
    direction this must never take, and the caller cannot distinguish "read
    fine, nothing matched" from "read failed" unless it is told.

    Reachable, but NOT for the reason an earlier version of this docstring
    gave. It claimed a timeout asymmetry — read at 5s, write at 10s — so a
    5-10s lock would fail the gate and pass the write. That is false, and was
    caught in review: the write path issues ``PRAGMA busy_timeout=5000``
    AFTER ``connect(timeout=10)``, and the PRAGMA wins. MEASURED: both paths
    report an effective busy_timeout of 5000 ms.

    The real reachability is plainer and does not depend on any number. This
    read happens up to EXTRACTOR_TIMEOUT_S before the write, on the far side
    of a Haiku call, so the two connections meet different database states;
    and it can fail outright for reasons the write path does not share — the
    shadow tables not existing yet in the pre-migration window, a ``mode=ro``
    handle unable to recover a hot WAL, or any I/O error. Frequency is not the
    argument. The argument is the fail DIRECTION: a best-effort read feeding a
    write-authority decision must fail closed on its own terms, whatever makes
    it fail.
    """
    import aiosqlite

    try:
        async with aiosqlite.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5) as db:
            db.row_factory = aiosqlite.Row
            # OPEN rows only, mirroring the transactional recheck in
            # _promote_live. Matching here against closed rows stamped a
            # renewal of a finished agreement `exact`/`fuzzy` at observation
            # time — and the sweep's `match_kind = 'none'` prefilter then
            # rejected it before the (already open-only) transactional check
            # could ever run, so the renewed commitment never promoted. The
            # report is unaffected: it recomputes matching itself against the
            # full foreground set and never trusts this stored verdict.
            items = await ledger_list(
                db, session_id, statuses=["open", "in_progress"]
            )
            priors = await shadow_crud.list_events(db, session_id)
            return items, priors, True
    except Exception:
        logger.warning(
            "ledger match context unreadable for %s — promotion will be skipped",
            session_id, exc_info=True,
        )
        return [], [], False


async def run_ledger_worker(
    session_id: str,
    transcript_path: str,
    end_byte: int,
    *,
    trigger: str = "unknown",
    claude_path: str = "claude",
    db_path: Path | str | None = None,
) -> dict:
    """One shadow extraction run. Returns the outcome dict, never raises."""
    outcome: dict = {"status": "failed", "detail": ""}
    try:
        outcome = await _run(
            session_id,
            transcript_path,
            end_byte,
            trigger=trigger,
            claude_path=claude_path,
            db_path=db_path or genesis_db_path(),
        )
    except Exception as exc:  # noqa: BLE001 — detached: record, never raise
        outcome = {"status": "failed", "detail": f"{type(exc).__name__}: {exc}"}
    return outcome


async def _run(
    session_id: str,
    transcript_path: str,
    end_byte: int,
    *,
    trigger: str,
    claude_path: str,
    db_path: Path | str,
) -> dict:
    if os.environ.get("GENESIS_LEDGER_SHADOW_DISABLED") == "1":
        return {"status": "skipped_disabled"}
    mode = effective_mode()
    if mode == "off":
        # No run row, no lock, cursor untouched — indistinguishable from
        # the feature not existing (the hook-side kill switch is the
        # cheaper lever; this one catches settings flips).
        return {"status": "skipped_off"}
    if not _SESSION_ID_RE.match(session_id):
        logger.warning("unsafe session id %r — skipping run", session_id)
        return {"status": "skipped_bad_session_id"}

    session_dir = _sessions_root() / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    started_at = _now()
    t0 = time.monotonic()

    lock_path = session_dir / LOCK_FILENAME
    lock_fh = lock_path.open("w")
    try:
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            run_id = uuid.uuid4().hex
            await _record_run(
                db_path,
                run_id=run_id,
                session_id=session_id,
                started_at=started_at,
                finished_at=_now(),
                start_byte=-1,
                end_byte=end_byte,
                trigger=trigger,
                status="lock_busy",
                mode=mode,
                # Stamped even though no extraction ran: the report scopes its
                # health population by version, and a current run that failed
                # BEFORE extraction is still a current run — leaving the column
                # NULL filed these rows as legacy and made worker health look
                # better precisely when it was failing early.
                prompt_version=PROMPT_VERSION,
            )
            return {"status": "lock_busy"}
        return await _run_locked(
            session_id,
            transcript_path,
            end_byte,
            trigger=trigger,
            claude_path=claude_path,
            db_path=db_path,
            session_dir=session_dir,
            started_at=started_at,
            t0=t0,
            mode=mode,
        )
    finally:
        lock_fh.close()


async def _run_locked(
    session_id: str,
    transcript_path: str,
    end_byte: int,
    *,
    trigger: str,
    claude_path: str,
    db_path: Path | str,
    session_dir: Path,
    started_at: str,
    t0: float,
    mode: str,
) -> dict:
    run_id = uuid.uuid4().hex
    detail_notes: list[str] = []
    cursor = _read_cursor(session_dir)
    start_byte = int(cursor.get("last_byte") or 0)

    transcript = Path(transcript_path)
    try:
        size = transcript.stat().st_size
    except OSError as exc:
        await _record_run(
            db_path,
            run_id=run_id,
            session_id=session_id,
            started_at=started_at,
            finished_at=_now(),
            start_byte=start_byte,
            end_byte=end_byte,
            trigger=trigger,
            status="failed",
            mode=mode,
            # Same rule as the lock_busy row: a current-version early failure
            # must stay in the current-version health population.
            prompt_version=PROMPT_VERSION,
            detail=f"transcript_unreadable: {exc}",
        )
        await _record_telemetry(db_path, "failed", "transcript_unreadable")
        return {"status": "failed", "detail": "transcript_unreadable"}

    if start_byte > size:
        # Shrunk/replaced transcript: never wedge permanently. The stale
        # byte must also leave the prior dict, or the monotonic advance
        # would max() against it and wedge the cursor forever.
        start_byte = 0
        cursor = dict(cursor, last_byte=0)
        detail_notes.append("cursor_beyond_eof_reset")
    end_byte = min(end_byte, size)
    if end_byte - start_byte > MAX_WINDOW_BYTES:
        # Keep-recent applies at the read layer too.
        start_byte = end_byte - MAX_WINDOW_BYTES
        detail_notes.append("window_capped")

    def _base_row(**over) -> dict:
        row = dict(
            run_id=run_id,
            session_id=session_id,
            started_at=started_at,
            finished_at=_now(),
            start_byte=start_byte,
            end_byte=end_byte,
            trigger=trigger,
            mode=mode,
            latency_ms=int((time.monotonic() - t0) * 1000),
            prompt_version=PROMPT_VERSION,
            model=EXTRACTOR_MODEL,
            detail="; ".join(detail_notes) or None,
        )
        row.update(over)
        return row

    if start_byte >= end_byte:
        recorded = await _record_run(db_path, **_base_row(status="empty_delta"))
        if recorded:
            _advance_cursor(session_dir, end_byte, cursor)
        await _record_telemetry(db_path, "empty_delta", "no_new_bytes")
        return {"status": "empty_delta"}

    # The extractor owns these budgets; parse_delta truncates FIRST, so its
    # defaults silently win unless they are passed. That is not hypothetical:
    # v2 raised ASSISTANT_SNIPPET_CHARS to 1400 while parse_delta kept cutting
    # at 500, so the whole point of the bump — giving the model the actual
    # proposal being ratified — never reached it.
    turns = parse_delta(
        transcript, start_byte, end_byte,
        max_user_chars=USER_TURN_CHARS,
        max_assistant_chars=ASSISTANT_SNIPPET_CHARS,
    )
    if not turns:
        recorded = await _record_run(db_path, **_base_row(status="empty_delta"))
        if recorded:
            _advance_cursor(session_dir, end_byte, cursor)
        await _record_telemetry(db_path, "empty_delta", "no_typed_turns")
        return {"status": "empty_delta"}

    prompt, included, truncated = build_prompt(turns)
    result = await run_headless_json(
        prompt,
        model=EXTRACTOR_MODEL,
        claude_path=claude_path,
        timeout_s=EXTRACTOR_TIMEOUT_S,
    )
    if result["status"] != "ok":
        status = "timeout" if result["status"] == "timeout" else "failed"
        if result.get("reason"):
            detail_notes.append(str(result["reason"])[:200])
        await _record_run(
            db_path,
            **_base_row(status=status, n_user_turns=len(included), truncated=truncated),
        )
        await _record_telemetry(db_path, status, str(result.get("reason") or ""))
        return {"status": status, "detail": result.get("reason")}

    verdict = parse_verdict(result["stdout"], len(included))
    if verdict is None:
        detail_notes.append("unparseable")
        await _record_run(
            db_path,
            **_base_row(status="failed", n_user_turns=len(included), truncated=truncated),
        )
        await _record_telemetry(db_path, "failed", "unparseable")
        return {"status": "failed", "detail": "unparseable"}

    ledger_items, prior_events, match_read_ok = await _load_match_context(
        db_path, session_id
    )
    events = match_proposals(verdict, included, ledger_items, prior_events)
    observed_at = _now()
    for ev in events:
        ev["observed_at"] = observed_at

    # Decide the promotion disposition BEFORE the run row is written.
    # `_base_row` renders detail_notes at CALL time, so anything appended after
    # this point never reaches session_ledger_shadow_runs.detail — the note is
    # written to a string that was already joined. That bug shipped twice here
    # (this branch's promotion note, and the pre-existing cursor-preserved
    # note below), which is why the ordering is now explicit rather than
    # incidental.
    promote_now = mode == "live"
    if promote_now and not match_read_ok:
        # Both novelty signals came from a read that failed, so everything
        # looks promotable. Skip rather than write on evidence that does not
        # exist; the shadow rows stay unpromoted and the next live run's sweep
        # retries them at no cost.
        detail_notes.append("promotion_skipped_match_context_unreadable")
        promote_now = False

    recorded = await _record_run(
        db_path,
        **_base_row(
            status="ok",
            n_user_turns=len(included),
            n_proposals=len(events),
            truncated=truncated,
        ),
        events=events,
    )
    if recorded:
        _advance_cursor(session_dir, end_byte, cursor)
    else:
        # The run row is already written, so this note cannot reach it — carry
        # the fact on the returned outcome and the telemetry line instead.
        detail_notes.append("shadow_write_failed_cursor_preserved")

    # Promotion runs only AFTER the shadow row is safely written: the shadow
    # store is the audit trail for what the extractor proposed, and it must
    # exist even if the live write then fails. It also has to run after the
    # duplicate marking that match_proposals stamped onto these events, which
    # is what makes a re-covered window idempotent. The sweep reads candidates
    # back from the shadow store (this run's rows included, plus any earlier
    # unpromoted leftovers), so promotion failure never needs the cursor to
    # move backwards — the shadow row IS the retry state.
    promotion: dict = {"promoted": 0}
    if recorded and promote_now:
        # Re-read the gate immediately before the only write. The mode read at
        # entry is up to EXTRACTOR_TIMEOUT_S (120s) old by now, and the
        # documented emergency rollback is `mode: shadow` — an operator doing
        # that mid-run should not still get this run's promotions. Free, and
        # fails in the safe direction.
        if effective_mode() != "live":
            detail_notes.append("promotion_skipped_mode_changed_midrun")
        else:
            promotion = await _promote_live(db_path, session_id, trigger=trigger)
    elif recorded and mode == "live" and not match_read_ok:
        promotion = {"promoted": 0, "skipped_unreadable_context": True}

    # Every counter reaches a surface an operator reads. A sweep where all
    # candidates raised, and one where nothing qualified, both used to record
    # `promoted=0` — byte-identical, with the only signal in a stderr log
    # nobody tails. A silent write failure reads downstream as "the extractor
    # found nothing", which is the exact misreading this whole subsystem's
    # cap-logging convention exists to prevent.
    # A run that FAILED to write is not an `ok` run, whichever write failed.
    # `recorded` covers only the SHADOW row, so a sweep whose every live
    # promotion raised still reported `ok` — to the neural monitor AND to
    # scripts/ledger_shadow_worker.py, which prints to stderr only on
    # failed/timeout and so stayed silent. The failure count was carried in the
    # detail string, where nothing reads it. The counters below still separate
    # "nothing qualified" from "everything failed"; the STATUS now does too.
    # `sweep_error` is what keeps an aborted sweep from wearing "nothing
    # qualified"'s clothes: both return zero counters, and only the flag says
    # the zeros were never computed. `mode_stopped` is informational, not a
    # failure — an operator rollback honoured mid-sweep is the design working.
    _write_failed = (
        (not recorded)
        or bool(promotion.get("failed_rows", 0))
        or bool(promotion.get("sweep_error"))
    )
    if promotion.get("mode_stopped"):
        detail_notes.append("promotion_stopped_mode_changed_midsweep")
    await _record_telemetry(
        db_path,
        "failed" if _write_failed else "ok",
        f"turns={len(included)}|proposals={len(events)}|recorded={recorded}"
        f"|qualifying={promotion.get('qualifying', 0)}"
        f"|promoted={promotion.get('promoted', 0)}"
        f"|disqualified={promotion.get('disqualified_at_write', 0)}"
        f"|promotion_failed={promotion.get('failed_rows', 0)}"
        f"|sweep_error={int(bool(promotion.get('sweep_error')))}"
        + (f"|{'; '.join(detail_notes)}" if detail_notes else ""),
    )
    return {
        "status": "failed" if _write_failed else "ok",
        "n_proposals": len(events),
        "recorded": recorded,
        "promoted": promotion.get("promoted", 0),
        "qualifying": promotion.get("qualifying", 0),
        "disqualified_at_write": promotion.get("disqualified_at_write", 0),
        "promotion_failed_rows": promotion.get("failed_rows", 0),
        "notes": list(detail_notes),
    }


# Backfill: historical sessions predate the waypoint/cursor spine, so
# windows slice by TYPED-TURN COUNT, not bytes.
BACKFILL_TURNS_PER_WINDOW = 20
BACKFILL_MAX_WINDOWS = 10


async def run_backfill(
    session_id: str,
    transcript_path: str,
    *,
    turns_per_window: int = BACKFILL_TURNS_PER_WINDOW,
    max_windows: int = BACKFILL_MAX_WINDOWS,
    claude_path: str = "claude",
    db_path: Path | str | None = None,
) -> dict:
    """Replay a historical transcript through the extractor (decision 5b).

    Slices the transcript's typed turns into windows of ``turns_per_window``
    and runs each through the same prompt→parse→match pipeline, NEWEST
    windows first-served (``max_windows`` cap bounds Haiku calls). Rows are
    tagged ``trigger='backfill'`` (the report excludes them from precision
    metrics by default — historical sessions have no foreground ground
    truth) and the live cursor file is NEVER touched. The per-session flock
    is still taken so a backfill can't race a live compaction run. Never
    raises.
    """
    try:
        return await _run_backfill(
            session_id,
            transcript_path,
            turns_per_window=turns_per_window,
            max_windows=max_windows,
            claude_path=claude_path,
            db_path=db_path or genesis_db_path(),
        )
    except Exception as exc:  # noqa: BLE001 — detached: record, never raise
        return {"status": "failed", "detail": f"{type(exc).__name__}: {exc}"}


async def _run_backfill(
    session_id: str,
    transcript_path: str,
    *,
    turns_per_window: int,
    max_windows: int,
    claude_path: str,
    db_path: Path | str,
) -> dict:
    if os.environ.get("GENESIS_LEDGER_SHADOW_DISABLED") == "1":
        return {"status": "skipped_disabled"}
    if effective_mode() == "off":
        return {"status": "skipped_off"}

    if not _SESSION_ID_RE.match(session_id):
        logger.warning("unsafe session id %r — skipping backfill", session_id)
        return {"status": "skipped_bad_session_id"}

    transcript = Path(transcript_path)
    try:
        size = transcript.stat().st_size
    except OSError as exc:
        return {"status": "failed", "detail": f"transcript_unreadable: {exc}"}

    session_dir = _sessions_root() / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    lock_fh = (session_dir / LOCK_FILENAME).open("w")
    try:
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return {"status": "lock_busy"}

        all_turns = parse_delta(
            transcript, 0, size,
            max_user_chars=USER_TURN_CHARS,
            max_assistant_chars=ASSISTANT_SNIPPET_CHARS,
        )
        if not all_turns:
            return {"status": "empty_delta", "windows": 0}
        windows = [
            all_turns[i : i + turns_per_window] for i in range(0, len(all_turns), turns_per_window)
        ]
        skipped = max(0, len(windows) - max_windows)
        windows = windows[-max_windows:]  # newest windows within the call cap

        # Backfill never promotes, so the read-ok flag is irrelevant here.
        ledger_items, prior_events, _ = await _load_match_context(
            db_path, session_id
        )
        priors = list(prior_events)
        outcomes: list[str] = []
        total_proposals = 0
        for idx, window in enumerate(windows):
            started_at = _now()
            t0 = time.monotonic()
            prompt, included, truncated = build_prompt(window)
            result = await run_headless_json(
                prompt,
                model=EXTRACTOR_MODEL,
                claude_path=claude_path,
                timeout_s=EXTRACTOR_TIMEOUT_S,
            )
            verdict = (
                parse_verdict(result["stdout"], len(included)) if result["status"] == "ok" else None
            )
            detail = f"backfill_window {idx + 1}/{len(windows)} (skipped_older={skipped})"
            if verdict is None:
                status = "timeout" if result["status"] == "timeout" else "failed"
                reason = result.get("reason") or ("unparseable" if result["status"] == "ok" else "")
                await _record_run(
                    db_path,
                    run_id=uuid.uuid4().hex,
                    session_id=session_id,
                    started_at=started_at,
                    finished_at=_now(),
                    start_byte=0,
                    end_byte=size,
                    trigger="backfill",
                    status=status,
                    n_user_turns=len(included),
                    truncated=truncated,
                    latency_ms=int((time.monotonic() - t0) * 1000),
                    prompt_version=PROMPT_VERSION,
                    model=EXTRACTOR_MODEL,
                    detail=f"{detail}; {reason}".strip("; "),
                )
                outcomes.append(status)
                continue
            events = match_proposals(verdict, included, ledger_items, priors)
            observed_at = _now()
            for ev in events:
                ev["observed_at"] = observed_at
            recorded = await _record_run(
                db_path,
                run_id=uuid.uuid4().hex,
                session_id=session_id,
                started_at=started_at,
                finished_at=_now(),
                start_byte=0,
                end_byte=size,
                trigger="backfill",
                status="ok",
                n_user_turns=len(included),
                n_proposals=len(events),
                truncated=truncated,
                latency_ms=int((time.monotonic() - t0) * 1000),
                prompt_version=PROMPT_VERSION,
                model=EXTRACTOR_MODEL,
                detail=detail,
                events=events,
            )
            if recorded:
                # cross-window dedup within this backfill
                priors.extend({"id": e["id"], "kind": e["kind"], "text": e["text"]} for e in events)
                total_proposals += len(events)
            outcomes.append("ok" if recorded else "failed")
        await _record_telemetry(
            db_path,
            "ok" if outcomes and all(o == "ok" for o in outcomes) else "failed",
            f"backfill windows={len(windows)}|proposals={total_proposals}",
        )
        if all(o == "ok" for o in outcomes):
            status = "ok"
        elif any(o == "ok" for o in outcomes):
            status = "partial"
        else:
            status = "failed"
        return {
            "status": status,
            "windows": len(windows),
            "outcomes": outcomes,
            "n_proposals": total_proposals,
        }
    finally:
        lock_fh.close()
