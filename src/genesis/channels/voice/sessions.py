"""In-memory voice session manager.

Sessions maintain a short conversation buffer so follow-up questions within
a 45-second window keep context.  All state is ephemeral — lost on restart,
which is acceptable for Phase 1 reactive voice.

Thread-safety: all methods are coroutines and MUST be called from the main
asyncio event loop (the same loop that Flask threads submit to via
``asyncio.run_coroutine_threadsafe``).  No locking needed because asyncio
coroutines don't preempt each other within a single loop.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

logger = logging.getLogger(__name__)

_DEFAULT_SUSTAIN_SECONDS = 45
_DEFAULT_MAX_TURNS = 15
_DEFAULT_MAX_DURATION_MINUTES = 10
_MAX_BUFFER_MESSAGES = 20  # Ring buffer limit


@dataclass
class VoiceSession:
    """A single voice conversation session."""

    session_id: str
    started_at: datetime
    last_activity: datetime
    turn_count: int = 0
    buffer: list[dict[str, str]] = field(default_factory=list)
    _expiry_handle: asyncio.TimerHandle | None = field(
        default=None, repr=False,
    )


class VoiceSessionManager:
    """Lightweight async session manager for voice conversations.

    Inspired by the Gatekeeper's session sustain pattern but simpler:
    in-memory only, no Redis, no persistence.
    """

    def __init__(
        self,
        *,
        sustain_seconds: int = _DEFAULT_SUSTAIN_SECONDS,
        max_turns: int = _DEFAULT_MAX_TURNS,
        max_duration_minutes: int = _DEFAULT_MAX_DURATION_MINUTES,
    ) -> None:
        self._sustain_seconds = sustain_seconds
        self._max_turns = max_turns
        self._max_duration_minutes = max_duration_minutes
        self._sessions: dict[str, VoiceSession] = {}

    async def get_or_create(self, session_id: str) -> VoiceSession:
        """Return existing session or create a new one.

        Resets the sustain timer on every access.  Expired or over-limit
        sessions are removed and a fresh one is created.
        """
        now = datetime.now(UTC)
        session = self._sessions.get(session_id)

        if session is not None:
            if self._is_expired(session, now):
                self._remove(session_id)
                session = None
            else:
                session.last_activity = now
                self._reset_timer(session)
                return session

        # Create new session
        session = VoiceSession(
            session_id=session_id,
            started_at=now,
            last_activity=now,
        )
        self._sessions[session_id] = session
        self._reset_timer(session)
        logger.info("Voice session created: %s", session_id[:12])
        return session

    async def add_turn(
        self, session_id: str, role: str, content: str,
    ) -> None:
        """Append a message to the session buffer."""
        session = self._sessions.get(session_id)
        if session is None:
            return
        session.buffer.append({"role": role, "content": content})
        # Ring buffer — drop oldest messages beyond limit
        if len(session.buffer) > _MAX_BUFFER_MESSAGES:
            session.buffer = session.buffer[-_MAX_BUFFER_MESSAGES:]
        if role == "user":
            session.turn_count += 1

    def get_buffer(self, session_id: str) -> list[dict[str, str]]:
        """Return the conversation buffer for a session (may be empty)."""
        session = self._sessions.get(session_id)
        if session is None:
            return []
        return list(session.buffer)

    @property
    def active_count(self) -> int:
        return len(self._sessions)

    def _is_expired(self, session: VoiceSession, now: datetime) -> bool:
        idle = (now - session.last_activity).total_seconds()
        duration = (now - session.started_at).total_seconds()
        return (
            idle > self._sustain_seconds
            or duration > self._max_duration_minutes * 60
            or session.turn_count >= self._max_turns
        )

    def _reset_timer(self, session: VoiceSession) -> None:
        """Cancel existing expiry timer and schedule a new one."""
        if session._expiry_handle is not None:
            session._expiry_handle.cancel()
        loop = asyncio.get_running_loop()
        session._expiry_handle = loop.call_later(
            self._sustain_seconds,
            self._expire_callback,
            session.session_id,
        )

    def _expire_callback(self, session_id: str) -> None:
        """Called by the event loop when the sustain timer fires."""
        if session_id in self._sessions:
            logger.info("Voice session expired: %s", session_id[:12])
            self._remove(session_id)

    def _remove(self, session_id: str) -> None:
        session = self._sessions.pop(session_id, None)
        if session and session._expiry_handle:
            session._expiry_handle.cancel()
