"""Cross-session recency resume for the S2S voice model.

Builds an age-stamped "Where we left off …" block from the TAIL of the user's
most-recent PRIOR voice conversation, for injection into the S2S system prompt
so the model can proactively pick the thread back up across sessions.

Why this exists: `ask_genesis` (voice recall) only searches the *extracted*
long-term memory index, which lags the extraction cycle (1-2h+) and has no
recency path — so "what were we just talking about?" is unreachable by any
existing path once the edge's 300s reconnect cache lapses. This reader goes
straight to the durable transcript file, independent of extraction.

Design constraints:
  - **SYNC.** `get_system_prompt` is synchronous (the Flask `/v1/voice/system_prompt`
    route that the edge fetches cannot await), so this uses a short-lived
    read-only sqlite connection + a direct file read — never an async coroutine.
  - **Time-keyed, not status-keyed.** The idle reaper that flips a voice session
    to ``completed`` fires at the SAME 300s as the edge cache, so a status filter
    would miss the prior conversation at the exact moment a new one begins. We
    select on ``last_activity_at`` (most-recently-active, older than a 60s gap to
    exclude a session being appended right now) and exclude the current session id.
  - **Fail-closed.** ANY error (no DB, busy lock, missing/quarantined transcript,
    parse failure) returns ``""`` so the 10s connect window is never jeopardized.

Gated by ``voice_recency_resume_config`` (ships ``off``).
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime, timedelta

from genesis.channels.voice import voice_recency_resume_config as _cfg
from genesis.env import genesis_db_path, voice_transcript_dir
from genesis.util.jsonl import read_transcript_messages

logger = logging.getLogger(__name__)

# How many recent voice sessions to consider before giving up — lets the lookup
# fall through a quarantined/pruned transcript to the next real one.
_CANDIDATES = 3
# Exclude a session appended within this window (the live one on another path).
_SELF_GAP_SECONDS = 60


def build_recency_block(
    *,
    current_session_id: str | None = None,
    satellite_id: str | None = None,
    db_path: str | None = None,
    now: datetime | None = None,
) -> str:
    """Age-stamped tail of the most-recent prior voice conversation, or ``""``.

    ``current_session_id`` is the uuid5 transcript/row id of the session being
    started (excluded from the search). ``db_path``/``now`` are injectable for
    tests. Never raises — fail-closed to ``""``.
    """
    cfg = _cfg.resolved()
    if cfg["mode"] != "live":
        return ""
    try:
        return _build(cfg, current_session_id, satellite_id, db_path, now)
    except Exception:
        logger.debug("voice recency-resume block build failed; skipping", exc_info=True)
        return ""


def _build(
    cfg: dict,
    current_session_id: str | None,
    satellite_id: str | None,
    db_path: str | None,
    now: datetime | None,
) -> str:
    now = now or datetime.now(UTC)
    where = ["source_tag = 'voice'", "last_activity_at IS NOT NULL", "last_activity_at < ?"]
    params: list[str] = [(now - timedelta(seconds=_SELF_GAP_SECONDS)).isoformat()]
    if current_session_id:
        where.append("id != ?")
        params.append(current_session_id)
    if cfg["scope"] == "per_device" and satellite_id:
        where.append("satellite_id = ?")
        params.append(satellite_id)
    if cfg["max_age_hours"] is not None:
        where.append("last_activity_at >= ?")
        params.append((now - timedelta(hours=cfg["max_age_hours"])).isoformat())
    # All WHERE fragments are hardcoded constants and all values (incl. LIMIT)
    # are bound via ? placeholders — no input reaches the SQL string itself.
    where_clause = " AND ".join(where)
    sql = (
        "SELECT id, last_activity_at FROM cc_sessions "  # noqa: S608
        f"WHERE {where_clause} "
        "ORDER BY last_activity_at DESC LIMIT ?"
    )

    path = str(db_path or genesis_db_path())
    # mode=ro is WAL-aware (immutable=1 would miss un-checkpointed writes); a
    # small timeout so a busy -wal can't stall the connect path.
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
    try:
        rows = conn.execute(sql, [*params, _CANDIDATES]).fetchall()
    finally:
        conn.close()

    tdir = voice_transcript_dir()
    for sid, last_activity in rows:
        tpath = tdir / f"{sid}.jsonl"
        if not tpath.exists():
            continue  # quarantined / pruned — fall through to the next candidate
        messages = read_transcript_messages(tpath)
        if not messages:
            continue
        lines: list[str] = []
        for msg in messages[-cfg["max_turns"] :]:
            text = (msg.text or "").strip()
            if not text:
                continue
            speaker = "You" if msg.role == "user" else "Genesis"
            lines.append(f"{speaker}: {text}")
        if not lines:
            continue
        body = _fit(lines, cfg["max_chars"])
        if not body:
            continue
        return _frame(body, last_activity, now)
    return ""


def _fit(lines: list[str], max_chars: int) -> str:
    """Keep the NEWEST turns that fit within ``max_chars`` (drop oldest first)."""
    kept: list[str] = []
    total = 0
    for line in reversed(lines):
        cost = len(line) + 1
        if kept and total + cost > max_chars:
            break
        kept.append(line)
        total += cost
    kept.reverse()
    return "\n".join(kept)[:max_chars].rstrip()


def _frame(body: str, last_activity: str | None, now: datetime) -> str:
    # Lazy import: _humanize_age lives in handler.py; import inside the function
    # to avoid any channels/voice import-cycle at module load.
    from genesis.channels.voice.handler import _humanize_age

    age = _humanize_age(last_activity, now=now)
    age_str = f" ({age})" if age else ""
    return (
        f"Where we left off{age_str} — pick this up naturally if it still fits; "
        f"if it's old, don't force it:\n{body}"
    )
