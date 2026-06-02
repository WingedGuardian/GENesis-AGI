"""Speech-to-speech session manager — GPT-Realtime integration.

Manages live WebSocket sessions to OpenAI's Realtime API.  Each session
maps to a satellite (Voice PE device) and handles bidirectional audio
streaming with function calling for Genesis backend access.

The session lifecycle:
1. connect() — open WebSocket, configure tools + system prompt
2. stream_audio(chunk) — forward PCM frames to the model
3. receive loop — yields audio response chunks + function calls
4. close() — tear down WebSocket, store transcriptions
"""

from __future__ import annotations

import asyncio
import base64
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime

try:
    import openai
except ImportError:
    openai = None  # type: ignore[assignment]  # optional dep (pip install genesis[voice])

from genesis.channels.voice import config as voice_config
from genesis.channels.voice.genesis_bridge import (
    TOOL_DECLARATIONS,
    GenesisBridge,
)

logger = logging.getLogger(__name__)


@dataclass
class S2SResponseEvent:
    """An event from the S2S model's response stream."""

    type: str  # "audio", "transcript", "function_call", "done", "error"
    audio: bytes | None = None  # PCM audio data (for type="audio")
    text: str = ""  # transcript text or error message
    function_name: str = ""  # for type="function_call"
    function_args: str = ""  # JSON string
    call_id: str = ""  # function call ID for sending results back


@dataclass
class S2SSession:
    """A single S2S voice session with the Realtime API."""

    session_id: str
    satellite_id: str
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    last_activity: datetime = field(default_factory=lambda: datetime.now(UTC))
    connection: object | None = None  # openai.AsyncRealtimeConnection (lazy typed)
    _conn_mgr: object | None = field(default=None, repr=False)
    input_transcript: str = ""
    output_transcript: str = ""
    turn_count: int = 0
    _closed: bool = False


class S2SSessionManager:
    """Manages GPT-Realtime WebSocket sessions for voice satellites.

    One session per satellite.  Sessions are reused for multi-turn
    conversations.  When a session expires or errors, it's replaced
    with a fresh one.
    """

    def __init__(
        self,
        *,
        bridge: GenesisBridge,
        max_idle_seconds: int = 60,
    ) -> None:
        self._bridge = bridge
        self._max_idle_seconds = max_idle_seconds
        self._sessions: dict[str, S2SSession] = {}
        self._client: openai.AsyncOpenAI | None = None
        self._reaper_task: asyncio.Task | None = None

    async def start_reaper(self) -> None:
        """Start the idle session reaper (runs every 15s)."""
        self._reaper_task = asyncio.create_task(
            self._reap_loop(), name="s2s-session-reaper",
        )

    async def _reap_loop(self) -> None:
        """Periodically close idle S2S sessions to prevent WebSocket/billing leaks."""
        while True:
            await asyncio.sleep(15)
            now = datetime.now(UTC)
            stale = [
                sat_id for sat_id, s in self._sessions.items()
                if (now - s.last_activity).total_seconds() > self._max_idle_seconds
                and not s._closed
            ]
            for sat_id in stale:
                logger.info("Reaping idle S2S session for satellite %s", sat_id)
                await self.close(sat_id)

    async def get_or_create(self, satellite_id: str) -> S2SSession:
        """Return an active session or create a new one."""
        session = self._sessions.get(satellite_id)
        if session and not session._closed and session.connection:
            session.last_activity = datetime.now(UTC)
            return session

        # Create new session
        if self._client is None:
            self._client = openai.AsyncOpenAI()

        session_id = f"s2s-{satellite_id}-{datetime.now(UTC).strftime('%H%M%S')}"
        session = S2SSession(session_id=session_id, satellite_id=satellite_id)
        self._sessions[satellite_id] = session
        logger.info("S2S session created: %s for satellite %s", session_id, satellite_id)
        return session

    async def connect(self, session: S2SSession) -> None:
        """Open WebSocket connection and configure the session."""
        if session.connection is not None:
            return

        model = voice_config.s2s_model()
        logger.info("Connecting to %s...", model)

        # The connection context manager is entered manually here
        # because the session outlives a single request
        conn_mgr = self._client.realtime.connect(model=model)
        conn = await conn_mgr.__aenter__()
        session.connection = conn
        session._conn_mgr = conn_mgr

        # Configure session with tools and system prompt
        system_prompt = self._bridge.get_system_prompt()
        await conn.session.update(session={
            "type": "realtime",
            "instructions": system_prompt,
            "tools": TOOL_DECLARATIONS,
        })

        # Wait for session.updated confirmation (timeout: 10s)
        try:
            async with asyncio.timeout(10):
                async for event in conn:
                    if event.type == "session.updated":
                        logger.info("S2S session configured: %s", session.session_id)
                        break
                    if event.type == "error":
                        msg = getattr(event, "error", event)
                        logger.error("S2S session config error: %s", msg)
                        raise RuntimeError(f"S2S session config failed: {msg}")
        except TimeoutError:
            logger.error("S2S session config timed out (10s)")
            raise RuntimeError("S2S session config timed out") from None

    async def send_audio(self, session: S2SSession, audio: bytes) -> None:
        """Send a PCM audio chunk to the S2S model.

        Audio format: 16-bit PCM, 16kHz, mono (matches Wyoming input).
        """
        if not session.connection or session._closed:
            return

        encoded = base64.b64encode(audio).decode()
        await session.connection.input_audio_buffer.append(audio=encoded)

    async def commit_audio(self, session: S2SSession) -> None:
        """Signal end of audio input — trigger model response."""
        if not session.connection or session._closed:
            return
        await session.connection.input_audio_buffer.commit()
        await session.connection.response.create()

    async def receive_response(
        self, session: S2SSession,
    ) -> AsyncIterator[S2SResponseEvent]:
        """Yield response events from the S2S model.

        Handles function calls internally: when the model calls a tool,
        this method dispatches it via GenesisBridge and sends the result
        back, then continues yielding audio/transcript events.
        """
        if not session.connection or session._closed:
            return

        conn = session.connection
        async for event in conn:
            etype = event.type

            # Function call completed — dispatch to Genesis
            if etype == "response.function_call_arguments.done":
                yield S2SResponseEvent(
                    type="function_call",
                    function_name=event.name,
                    function_args=event.arguments,
                    call_id=event.call_id,
                )

                # Dispatch the tool call
                result = await self._bridge.handle_tool_call(
                    event.name, event.arguments,
                )

                # Send result back to the model
                await conn.conversation.item.create(item={
                    "type": "function_call_output",
                    "call_id": event.call_id,
                    "output": result,
                })
                await conn.response.create()

            # Audio data
            elif etype == "response.audio.delta":
                audio_bytes = base64.b64decode(event.delta)
                yield S2SResponseEvent(type="audio", audio=audio_bytes)

            # Audio transcript (what the model is saying)
            elif etype in (
                "response.audio_transcript.delta",
                "response.output_audio_transcript.delta",
            ):
                session.output_transcript += event.delta
                yield S2SResponseEvent(type="transcript", text=event.delta)

            # Input transcript (what the user said)
            elif etype == "input_audio_buffer.speech_started":
                session.input_transcript = ""

            elif etype in (
                "conversation.item.input_audio_transcription.completed",
                "conversation.item.input_audio_transcription.delta",
            ):
                if hasattr(event, "transcript"):
                    session.input_transcript += event.transcript

            # Response complete — add turn separator for transcript accumulation
            elif etype == "response.done":
                session.turn_count += 1
                session.last_activity = datetime.now(UTC)
                session.output_transcript += "\n"  # Turn boundary
                yield S2SResponseEvent(type="done")
                break

            # Error
            elif etype == "error":
                msg = str(getattr(event, "error", event))
                logger.error("S2S error: %s", msg)
                yield S2SResponseEvent(type="error", text=msg)
                break

    async def close(self, satellite_id: str) -> tuple[str, str]:
        """Close a session and return (input_transcript, output_transcript)."""
        session = self._sessions.pop(satellite_id, None)
        if not session:
            return "", ""

        session._closed = True
        transcripts = (session.input_transcript, session.output_transcript)

        if session.connection:
            try:
                conn_mgr = session._conn_mgr
                if conn_mgr:
                    await conn_mgr.__aexit__(None, None, None)
            except Exception:
                logger.exception("Error closing S2S session %s", session.session_id)

        logger.info(
            "S2S session closed: %s (turns=%d)",
            session.session_id, session.turn_count,
        )
        return transcripts

    async def close_all(self) -> None:
        """Close all active sessions and stop the reaper (graceful shutdown)."""
        if self._reaper_task and not self._reaper_task.done():
            self._reaper_task.cancel()
        for sat_id in list(self._sessions):
            await self.close(sat_id)
