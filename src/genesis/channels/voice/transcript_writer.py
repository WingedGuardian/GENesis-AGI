"""Voice conversation transcript writer — the extraction-parity landing.

S2S voice conversations used to land in episodic memory as one growing blob
per session close, bypassing the extraction pipeline every other channel goes
through. This writer replaces that landing: each conversation becomes a
per-session JSONL transcript in ``voice_transcript_dir()`` (CC-transcript
format, the exact shape ``genesis.util.jsonl.read_transcript_messages``
parses) plus a ``cc_sessions`` row (``source_tag='voice'``), so the memory
extraction job mines voice conversations incrementally by watermark — facts,
references and topics instead of raw dumps.

Two producers share this component:
- the core ``S2SSessionManager`` (per-turn ``append_message`` calls), and
- the ``POST /v1/voice/conversation`` route, targeted by the edge s2s bridge
  (``sync_cumulative`` with the bridge's full cached turn list).

Idempotency: session ids are deterministic (uuid5 of the external session
id) and ``sync_cumulative`` appends only turns beyond the file's current
line count — replays, double-fires and cumulative re-sends land exactly
once. Appends run under an ``asyncio.Lock``: the ``/v1/voice/conversation``
route dispatches each request on its own WSGI thread onto the shared runtime
loop, so without the lock two same-session cumulative syncs could both read
line count N (across the ``await`` in the critical section) and both append
the same delta — duplicating turns. All methods MUST be called on that loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

from genesis.db.crud import cc_sessions as sessions_crud
from genesis.env import voice_transcript_dir

logger = logging.getLogger(__name__)

# Deterministic namespace for external-session-id → transcript-session-id.
_VOICE_SESSION_NS = uuid.uuid5(uuid.NAMESPACE_URL, "genesis://voice/transcripts")

_ROLES = ("user", "assistant")
_MAX_SESSION_ID_LEN = 128
_MAX_TURNS = 5000


def validate_conversation(data: dict) -> list[str]:
    """Validate a /v1/voice/conversation body. Returns error strings ([] = valid)."""
    errors: list[str] = []

    session_id = data.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        errors.append("session_id: required non-empty string")
    elif len(session_id) > _MAX_SESSION_ID_LEN:
        errors.append(f"session_id: exceeds {_MAX_SESSION_ID_LEN} chars")

    satellite_id = data.get("satellite_id")
    if satellite_id is not None and not isinstance(satellite_id, str):
        errors.append("satellite_id: must be a string when present")

    turns = data.get("turns")
    if not isinstance(turns, list):
        errors.append("turns: required list")
        return errors
    if len(turns) > _MAX_TURNS:
        errors.append(f"turns: exceeds {_MAX_TURNS} entries")
        return errors
    for i, turn in enumerate(turns):
        if not isinstance(turn, dict):
            errors.append(f"turns[{i}]: must be an object")
            continue
        if turn.get("role") not in _ROLES:
            errors.append(f"turns[{i}].role: must be one of {_ROLES}")
        text = turn.get("text")
        if not isinstance(text, str) or not text.strip():
            errors.append(f"turns[{i}].text: required non-empty string")

    return errors


def transcript_session_id(external_session_id: str) -> str:
    """Deterministic transcript/cc_sessions id for an external session id."""
    return str(uuid.uuid5(_VOICE_SESSION_NS, external_session_id))


_shared_writer: VoiceTranscriptWriter | None = None


async def get_shared_writer(db: aiosqlite.Connection) -> VoiceTranscriptWriter:
    """Process-wide writer for the runtime DB (route + S2S manager share it).

    Lazy so the ``/v1/voice/conversation`` route works even on installs where
    the core S2S pipeline never initializes (the edge bridge posts over HTTP
    regardless). Heals orphaned 'active' voice rows on first construction;
    the daily voice hygiene job repeats the heal, so a boot with no voice
    activity still converges. Must be called on the runtime event loop.
    """
    global _shared_writer
    if _shared_writer is None or _shared_writer._db is not db:
        writer = VoiceTranscriptWriter(db)
        await writer.heal_orphans()
        _shared_writer = writer
    return _shared_writer


class VoiceTranscriptWriter:
    """Writes voice conversations as extractable CC-format transcripts."""

    def __init__(
        self,
        db: aiosqlite.Connection,
        *,
        transcript_dir: Path | None = None,
    ) -> None:
        self._db = db
        self._dir = transcript_dir or voice_transcript_dir()
        self._registered: set[str] = set()
        # Serializes the read-modify-write in append_message / sync_cumulative
        # so concurrent same-session requests on the shared loop cannot
        # interleave a line-count read with another append (double-write).
        self._lock = asyncio.Lock()

    async def heal_orphans(self) -> int:
        """Complete 'active' voice rows idle >1h (boot + daily self-heal).

        The idle gate keeps a conversation that is live RIGHT NOW (writer
        constructed lazily mid-uptime, or the daily hygiene tick) from having
        its row flipped mid-call; appends refresh last_activity_at.
        """
        from datetime import timedelta

        cutoff = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        healed = await sessions_crud.complete_orphaned_voice_sessions(
            self._db,
            idle_before=cutoff,
        )
        if healed:
            logger.info("Completed %d orphaned voice session row(s)", healed)
        return healed

    async def append_message(
        self,
        external_session_id: str,
        role: str,
        text: str,
    ) -> None:
        """Append one message to a session's transcript (core per-turn path)."""
        if role not in _ROLES or not text.strip():
            return
        sid = transcript_session_id(external_session_id)
        async with self._lock:
            await self._ensure_registered(sid)
            self._append_lines(sid, [(role, text)])
            await self._touch_activity(sid)

    async def sync_cumulative(
        self,
        external_session_id: str,
        turns: list[dict],
    ) -> int:
        """Reconcile a full cumulative turn list against the transcript.

        Appends only turns beyond the file's current message count, making
        replays and double-fires idempotent. A list SHORTER than the file
        violates the producer contract (the caller must regenerate its
        session id whenever its turn cache resets) — logged loudly, nothing
        appended. Returns the number of messages appended.
        """
        sid = transcript_session_id(external_session_id)

        # The read (line count) and the append MUST be atomic w.r.t. other
        # coroutines: concurrent same-session syncs on the shared loop would
        # otherwise both read N and both append turns[N:] (double-write).
        async with self._lock:
            existing = self._line_count(sid)
            if len(turns) < existing:
                logger.warning(
                    "Voice cumulative sync for %s sent %d turns but transcript "
                    "already has %d — producer turn cache reset without a new "
                    "session id; appending nothing",
                    external_session_id[:32],
                    len(turns),
                    existing,
                )
                return 0

            new_turns = [(t["role"], t["text"]) for t in turns[existing:]]
            if new_turns:
                await self._ensure_registered(sid)
                self._append_lines(sid, new_turns)
                await self._touch_activity(sid)
            return len(new_turns)

    async def close_session(self, external_session_id: str) -> None:
        """Mark a session's row completed (transcript stays for extraction)."""
        sid = transcript_session_id(external_session_id)
        await sessions_crud.update_status(self._db, sid, status="completed")

    # ── internals ────────────────────────────────────────────────────

    async def _ensure_registered(self, sid: str) -> None:
        if sid in self._registered:
            return
        now = datetime.now(UTC).isoformat()
        await sessions_crud.register_voice_session(
            self._db,
            id=sid,
            started_at=now,
        )
        self._registered.add(sid)

    async def _touch_activity(self, sid: str) -> None:
        await sessions_crud.update_activity(
            self._db,
            sid,
            last_activity_at=datetime.now(UTC).isoformat(),
        )

    def _path(self, sid: str) -> Path:
        return self._dir / f"{sid}.jsonl"

    def _line_count(self, sid: str) -> int:
        path = self._path(sid)
        if not path.exists():
            return 0
        # An unreadable file must NOT read as "0 lines" — that would make
        # the next cumulative sync re-append the whole conversation. Let the
        # OSError propagate (route answers 500; the producer retries).
        with open(path, encoding="utf-8") as f:
            return sum(1 for _ in f)

    def _append_lines(self, sid: str, turns: list[tuple[str, str]]) -> None:
        """Append CC-transcript-format JSONL lines (the shape
        ``read_transcript_messages`` parses: user content is a plain string,
        assistant content is a list of text blocks)."""
        self._dir.mkdir(parents=True, exist_ok=True)
        now = datetime.now(UTC).isoformat()
        lines = []
        for role, text in turns:
            if role == "user":
                message: dict = {"content": text}
            else:
                message = {"content": [{"type": "text", "text": text}]}
            lines.append(
                json.dumps(
                    {"type": role, "message": message, "timestamp": now},
                )
            )
        # Durable-before-ack: an append failure must RAISE so the route
        # answers 5xx and the producer retries — a swallowed OSError here
        # would return 200 to a caller that then discards its turn cache.
        path = self._path(sid)
        prefix = ""
        if path.exists() and path.stat().st_size > 0:
            # Crash-guard: a torn final line without a trailing newline must
            # not have the next message concatenated onto it.
            with open(path, "rb") as f:
                f.seek(-1, 2)
                if f.read(1) != b"\n":
                    prefix = "\n"
        with open(path, "a", encoding="utf-8") as f:
            f.write(prefix + "\n".join(lines) + "\n")
