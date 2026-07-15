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
    EXTRACTOR_MODEL,
    EXTRACTOR_TIMEOUT_S,
    PROMPT_VERSION,
    build_prompt,
    match_proposals,
    parse_verdict,
)
from genesis.session_awareness.ledger_shadow_config import effective_mode
from genesis.session_awareness.transcript import parse_delta

CURSOR_FILENAME = "ledger_shadow_cursor.json"
LOCK_FILENAME = "ledger_shadow.lock"

# Defensive ceiling on a single delta read: parse_delta streams line-by-line
# so memory stays flat, but an unbounded window on a monster transcript is
# still wasted work — the prompt keeps only ~24k chars of the NEWEST turns
# anyway, so cap the scanned window to the trailing span.
MAX_WINDOW_BYTES = 64 * 1024 * 1024


def _sessions_root() -> Path:
    return Path.home() / ".genesis" / "sessions"


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


async def _load_match_context(db_path: Path | str, session_id: str) -> tuple[list, list]:
    """(live ledger items, prior shadow events) for the match stage.

    Best-effort reads: on any failure the extractor still runs — proposals
    just carry match_kind='none' (the report recomputes matching offline
    anyway; stored match_kind is the at-run-time signal only).
    """
    import aiosqlite

    try:
        async with aiosqlite.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5) as db:
            db.row_factory = aiosqlite.Row
            items = await ledger_list(db, session_id)
            priors = await shadow_crud.list_events(db, session_id)
            return items, priors
    except Exception:
        return [], []


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

    turns = parse_delta(transcript, start_byte, end_byte)
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

    ledger_items, prior_events = await _load_match_context(db_path, session_id)
    events = match_proposals(verdict, included, ledger_items, prior_events)
    observed_at = _now()
    for ev in events:
        ev["observed_at"] = observed_at

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
        detail_notes.append("shadow_write_failed_cursor_preserved")
    await _record_telemetry(
        db_path,
        "ok" if recorded else "failed",
        f"turns={len(included)}|proposals={len(events)}|recorded={recorded}",
    )
    return {
        "status": "ok" if recorded else "failed",
        "n_proposals": len(events),
        "recorded": recorded,
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

        all_turns = parse_delta(transcript, 0, size)
        if not all_turns:
            return {"status": "empty_delta", "windows": 0}
        windows = [
            all_turns[i : i + turns_per_window] for i in range(0, len(all_turns), turns_per_window)
        ]
        skipped = max(0, len(windows) - max_windows)
        windows = windows[-max_windows:]  # newest windows within the call cap

        ledger_items, prior_events = await _load_match_context(db_path, session_id)
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
        return {
            "status": "ok",
            "windows": len(windows),
            "outcomes": outcomes,
            "n_proposals": total_proposals,
        }
    finally:
        lock_fh.close()
