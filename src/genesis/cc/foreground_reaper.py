"""Foreground CC-session liveness reaper (D3).

A foreground ``cc_sessions`` row stays ``status='active'`` by design after a
successful turn so the next turn can ``--resume`` it. There is no guaranteed
terminal write, so a crash / OOM / container restart mid-turn leaves the row
``active`` forever — indistinguishable from a healthy resumable session (the
2026-07-20 silent-death: a foreground turn launched background work, said "I'll
report back", was killed, and its row sat ``active`` 26h+ with the user never
told). Nothing reaps foreground rows: the ``session_reaper`` job hard-excludes
them (``query_stale`` filters ``session_type != 'foreground'``).

This reaper closes that gap WITHOUT breaking resume. Each pass:
  1. Finds foreground rows idle > ``idle_hours`` (``query_stale_foreground``).
  2. Relabels each ``active`` → ``checkpointed`` (``checkpoint_dark``,
     rowcount-guarded). Non-destructive: ``get_active_foreground`` matches
     ``checkpointed`` and ``get_or_create_foreground`` flips it back to ``active``
     on reuse, so ``--resume`` is untouched.
  3. Classifies the transcript tail (``dark_signal``) and, on the CRISP
     "unanswered user turn" signal — gated on the tail turn's OWN age and not
     already covered by the rate-limit / dispatch machinery — notifies the
     origin user their request was interrupted. The FUZZY promise-regex signal
     is shadow-logged only (never notifies) until its precision is measured.

Observability-only: it never re-dispatches or auto-retries the dead work. The
whole pass is gated by ``cc_foreground_reaper`` (``off | observe | notify``).
Wired into the existing ``session_reaper`` job in its OWN inner try/except so a
notify failure can never abort the stale-session / heartbeat cleanup.
"""

from __future__ import annotations

import html as html_mod
import json
import logging
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from genesis.cc.foreground_reaper_config import effective_mode, knob_int, load_config
from genesis.db.crud import cc_sessions, observations
from genesis.env import cc_project_dir
from genesis.session_awareness.transcript import _assistant_text, typed_prompt_text

logger = logging.getLogger(__name__)

# Narrow "promised deferred work" regex — SHADOW ONLY (never notifies) until its
# precision is measured against real dark rows. Deliberately tight to the
# report-back shapes Genesis actually emits, not generic sign-offs.
_PROMISE_RE = re.compile(
    r"(report back|get back to you|running in the background|"
    r"in the background and will|i['’]?ll (let you know|update you|follow up)|"
    r"once (it|this|that) (finishes|completes|is done|wraps up))",
    re.IGNORECASE,
)

# Retention for routine (low-priority) dark-session observations.
_OBS_TTL_DAYS = 45
_TAIL_MAX_BYTES = 262_144  # read at most the last 256 KB of a transcript
_TAIL_MAX_ENTRIES = 80  # classify over at most the last 80 parsed entries


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested without I/O or a runtime)
# ---------------------------------------------------------------------------
def dark_signal(entries: list[dict]) -> tuple[str, str | None]:
    """Classify a transcript tail. Returns ``(signal, last_user_ts)``.

    - ``unanswered_user`` — the last typed user prompt has NO assistant text
      reply after it (a turn that died before answering). CRISP → eligible to
      notify (subject to the age guard + covered-by check in the reaper).
    - ``promise`` — the last user turn WAS answered, but the reply promised
      deferred work ("I'll report back") that may have died. FUZZY → shadow only.
    - ``clean`` — answered, no deferral promise (a normal walk-away).
    - ``unknown`` — no typed user turn found (e.g. no transcript / earliest-crash
      session with no ``cc_session_id``).

    ``last_user_ts`` is the ISO timestamp of the last user turn (for the age
    guard), or None.
    """
    last_user_idx = -1
    last_user_ts: str | None = None
    for i, entry in enumerate(entries):
        if typed_prompt_text(entry) is not None:
            last_user_idx = i
            last_user_ts = str(entry.get("timestamp") or "") or None
    if last_user_idx == -1:
        return "unknown", None
    reply: str | None = None
    for entry in entries[last_user_idx + 1 :]:
        txt = _assistant_text(entry)
        if txt:
            reply = txt
    if reply is None:
        return "unanswered_user", last_user_ts
    if _PROMISE_RE.search(reply):
        return "promise", last_user_ts
    return "clean", last_user_ts


def _ts_older_than(ts_iso: str | None, cutoff: datetime) -> bool:
    """True iff ``ts_iso`` parses to a time at/before ``cutoff`` (age guard).

    A missing/unparseable timestamp is treated as NOT-old — we never notify on a
    turn whose age we cannot establish (conservative against the mid-flight FP).
    """
    if not ts_iso:
        return False
    try:
        dt = datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
    except ValueError:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt <= cutoff


def _transcript_path(cc_session_id: str) -> Path:
    """Path to a foreground session's CC transcript jsonl.

    Foreground sessions run in the repo root, so the project dir is
    ``cc_project_dir()`` — the same resolver the memory-extraction job uses.
    """
    return Path.home() / ".claude" / "projects" / cc_project_dir() / f"{cc_session_id}.jsonl"


def _read_tail_entries(
    path: Path,
    *,
    max_bytes: int = _TAIL_MAX_BYTES,
    max_entries: int = _TAIL_MAX_ENTRIES,
) -> list[dict]:
    """Last decoded JSONL entries of a transcript (bounded), oldest-first.

    Reads only the trailing ``max_bytes`` and drops a possibly-torn first line;
    returns at most ``max_entries`` decoded dict entries. Missing/unreadable →
    ``[]`` (a session with no transcript simply yields the ``unknown`` signal).
    """
    try:
        size = path.stat().st_size
    except OSError:
        return []
    try:
        with path.open("rb") as fh:
            if size > max_bytes:
                fh.seek(size - max_bytes)
                fh.readline()  # discard the torn partial line at the seek point
            raw_lines = fh.read().splitlines()
    except OSError:
        return []
    entries: list[dict] = []
    for raw in raw_lines:
        try:
            entry = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries[-max_entries:]


# ---------------------------------------------------------------------------
# Async steps
# ---------------------------------------------------------------------------
async def _covered_by_other_subsystem(db: Any, origin_session_id: str) -> bool:
    """True iff the rate-limit park or dispatch machinery already owns notifying
    the user about this session's work — so the reaper must not double-notify."""
    from genesis.db.crud import cc_rate_limit_parks, direct_session_queue

    return await cc_rate_limit_parks.has_open_for_origin(
        db, origin_session_id
    ) or await direct_session_queue.has_open_for_origin(db, origin_session_id)


async def _notify_origin(rt: Any, row: dict) -> bool:
    """Tell the origin user their earlier request was interrupted.

    Reuses the shipped delivery path (``_resolve_origin_target`` +
    ``submit_urgent(verbatim=True)``). Returns True iff a notify was submitted.
    Best-effort — the caller isolates exceptions. Non-addressable origins
    (non-Telegram / legacy) get no notify; the observation is the only record.
    """
    pipeline = getattr(rt, "_outreach_pipeline", None)
    if pipeline is None:
        return False
    from genesis.cc.direct_session import DirectSessionRunner

    chat_id, thread_id = DirectSessionRunner._resolve_origin_target(
        row.get("channel"),
        row.get("chat_id"),
        row.get("user_id") or "",
        row.get("thread_id"),
        getattr(pipeline, "_forum_chat_id", None),
    )
    if not chat_id:
        return False
    topic_hint = (row.get("topic") or "").strip()
    hint = f' ("{html_mod.escape(topic_hint[:80])}")' if topic_hint else ""
    text = (
        "<b>⚠️ Earlier request interrupted</b>\n\n"
        f"Your earlier request{hint} looks like it was interrupted before I "
        "finished, and nothing is still running on it. If you still want it, "
        "please re-send."
    )
    from genesis.outreach.types import OutreachCategory, OutreachRequest

    try:
        await pipeline.submit_urgent(
            OutreachRequest(
                category=OutreachCategory.ALERT,
                # Unique per session so governance dedup never collides.
                topic=f"dark_session:{row['id']}",
                context=text,
                salience_score=0.85,
                channel="telegram",
                verbatim=True,
                target_chat_id=chat_id,
                target_thread_id=thread_id,
            )
        )
    except Exception:
        # Delivery is best-effort and must NOT propagate: the row is already
        # checkpointed, so a raise would drop the caller's high-priority
        # observation and the alert would be lost forever (never re-selected).
        # The eligibility-driven high-priority observation still surfaces it.
        logger.warning(
            "foreground reaper: dark-session notify delivery failed for %s",
            row["id"][:8],
            exc_info=True,
        )
        return False
    return True


async def _observe(
    db: Any,
    row: dict,
    *,
    signal: str,
    notify_eligible: bool,
    notified: bool,
    shadow: bool,
    now: datetime,
) -> None:
    """Record a dark-session observation.

    Priority is driven by ELIGIBILITY, not delivery success: a crisp dead
    request (``notify_eligible``) is `high` — so it surfaces in the morning
    report EVEN IF the direct Telegram delivery raised/failed (the alert is
    never silently lost). Everything else (shadow promise, mid-flight, covered)
    is `low` + TTL-bounded.
    """
    priority = "high" if notify_eligible else "low"
    expires_at = None if notify_eligible else (now + timedelta(days=_OBS_TTL_DAYS)).isoformat()
    marker = "shadow " if shadow else ""
    if not notify_eligible:
        disposition = "not eligible"
    elif notified:
        disposition = "notified"
    else:
        disposition = "delivery failed (surfaced via this observation)"
    content = (
        f"Dark foreground session reaped to 'checkpointed': {row['id'][:8]} "
        f"(channel={row.get('channel')}, last_activity_at={row.get('last_activity_at')}); "
        f"signal={signal}; {marker}notify {disposition}."
    )
    try:
        await observations.create(
            db,
            id=str(uuid.uuid4()),
            source="foreground_reaper",
            type="dark_foreground_session",
            content=content,
            priority=priority,
            created_at=now.isoformat(),
            expires_at=expires_at,
        )
    except Exception:
        logger.warning(
            "foreground reaper: observation write failed for %s",
            row["id"][:8],
            exc_info=True,
        )


async def _process_row(
    rt: Any,
    db: Any,
    row: dict,
    *,
    now: datetime,
    cutoff: datetime,
    mode: str,
    result: dict,
) -> None:
    """Reap one dark row (checkpoint → classify → observe/notify). Isolated per
    row by the caller so one bad row cannot abort the pass."""
    won = await cc_sessions.checkpoint_dark(db, row["id"], checkpointed_at=now.isoformat())
    if not won:
        # A concurrent turn revived the row between the query and this write.
        return
    result["reaped"] += 1

    signal, tail_ts = "unknown", None
    cc_sid = row.get("cc_session_id")
    if cc_sid:
        signal, tail_ts = dark_signal(_read_tail_entries(_transcript_path(cc_sid)))

    shadow = signal == "promise"
    if shadow:
        result["shadow"] += 1

    # Notify-eligible = a CRISP dead request we should tell the user about:
    # unanswered user turn, in notify mode, the turn itself older than the idle
    # cutoff (age guard — excludes a mid-flight long turn whose session-level
    # last_activity is merely stale), and not already owned by the rate-limit
    # park / dispatch machinery.
    notify_eligible = (
        signal == "unanswered_user"
        and mode == "notify"
        and _ts_older_than(tail_ts, cutoff)
        and not await _covered_by_other_subsystem(db, row["id"])
    )
    notified = False
    if notify_eligible:
        notified = await _notify_origin(rt, row)  # never raises; False on failure
        if notified:
            result["notified"] += 1

    # Observe only the noteworthy signals; a routine 'clean'/'unknown' reap is
    # silent hygiene. Priority is eligibility-driven (see _observe) so a failed
    # delivery still surfaces the dead request via the morning report.
    if signal in ("unanswered_user", "promise"):
        await _observe(
            db,
            row,
            signal=signal,
            notify_eligible=notify_eligible,
            notified=notified,
            shadow=shadow,
            now=now,
        )


async def reap_dark_foreground(
    rt: Any,
    *,
    now: datetime | None = None,
    idle_hours: int | None = None,
    mode: str | None = None,
) -> dict:
    """One reaper pass. Returns a summary dict (mode/scanned/reaped/notified/shadow).

    Safe to call from the ``session_reaper`` job. Never raises for a per-row
    problem (each row is isolated); a catastrophic failure (bad db) surfaces to
    the caller's try/except.
    """
    result = {"mode": None, "scanned": 0, "reaped": 0, "notified": 0, "shadow": 0}
    db = getattr(rt, "_db", None)
    if db is None:
        return result

    mode = mode or effective_mode()
    result["mode"] = mode
    if mode == "off":
        return result

    cfg = load_config()
    if idle_hours is None:
        idle_hours = knob_int(cfg, "idle_hours")
    max_per_tick = knob_int(cfg, "max_per_tick")
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(hours=idle_hours)

    rows = await cc_sessions.query_stale_foreground(db, older_than=cutoff.isoformat())
    result["scanned"] = len(rows)
    if len(rows) > max_per_tick:
        logger.warning(
            "foreground reaper: %d dark rows exceed max_per_tick=%d — capping "
            "(remainder next pass)",
            len(rows),
            max_per_tick,
        )
        rows = rows[:max_per_tick]

    for row in rows:
        try:
            await _process_row(rt, db, row, now=now, cutoff=cutoff, mode=mode, result=result)
        except Exception:
            logger.warning(
                "foreground reaper: processing failed for %s",
                row.get("id", "?")[:8],
                exc_info=True,
            )

    if result["reaped"]:
        logger.info(
            "foreground reaper: checkpointed %d dark session(s) (notified=%d, shadow=%d, mode=%s)",
            result["reaped"],
            result["notified"],
            result["shadow"],
            mode,
        )
    return result
