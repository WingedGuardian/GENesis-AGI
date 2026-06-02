"""Speech-to-speech session manager — GPT-Realtime integration.

Manages live WebSocket sessions to OpenAI's Realtime API.  Each session
maps to a satellite (Voice PE device) and handles bidirectional audio
streaming with function calling for Genesis backend access.

The session lifecycle:
1. connect() — open WebSocket, configure tools + system prompt
2. send_turn(audio) — send a complete utterance, collect full response
3. close() — tear down WebSocket, store transcriptions

Audio flow (VAD disabled — we receive complete utterances from Wyoming):
  - Send bulk audio via conversation.item.create (input_audio content)
  - Call response.create() to trigger inference
  - Listen for response.done to detect function calls or audio output
  - Function calls: send result, call response.create() again
  - Audio: collect response.output_audio.delta chunks
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

        conn_mgr = self._client.realtime.connect(model=model)
        conn = await conn_mgr.__aenter__()
        session.connection = conn
        session._conn_mgr = conn_mgr

        # Minimal config — defaults give us audio output (alloy voice),
        # server VAD, 24kHz PCM. We use conversation.item.create for audio
        # input so VAD doesn't interfere with bulk utterances from Wyoming.
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
                        session.connection = None
                        raise RuntimeError(f"S2S session config failed: {msg}")
        except TimeoutError:
            logger.error("S2S session config timed out (10s)")
            session.connection = None
            raise RuntimeError("S2S session config timed out") from None

    async def send_turn(
        self, session: S2SSession, audio: bytes, *, input_rate: int = 16000,
    ) -> None:
        """Send a complete utterance as a conversation item and trigger a response.

        Uses conversation.item.create with input_audio content — cleaner than
        input_audio_buffer.append + commit for bulk audio (no VAD conflicts).
        Resamples from input_rate (default 16kHz from Wyoming) to 24kHz.
        """
        if not session.connection or session._closed:
            return

        # Resample 16kHz → 24kHz (Realtime API requires 24kHz PCM)
        if input_rate != 24000:
            from genesis.channels.voice.wyoming_tts import _resample_pcm
            audio = _resample_pcm(audio, input_rate, 24000)

        encoded = base64.b64encode(audio).decode()

        # Send as a conversation item (bypasses input_audio_buffer entirely)
        await session.connection.conversation.item.create(item={
            "type": "message",
            "role": "user",
            "content": [{"type": "input_audio", "audio": encoded}],
        })
        await session.connection.response.create()

    async def receive_response(
        self, session: S2SSession,
    ) -> AsyncIterator[S2SResponseEvent]:
        """Yield response events from the S2S model.

        Handles function calls internally: when the model calls a tool,
        this method dispatches it via GenesisBridge and sends the result
        back, then continues yielding audio/transcript events from the
        follow-up response.

        GPT-Realtime event sequence:
          Response 1 (function call): response.done with output[0].type == "function_call"
          → we send function_call_output + response.create()
          Response 2 (audio): response.output_audio.delta chunks → response.done
        """
        if not session.connection or session._closed:
            return

        conn = session.connection

        async for event in conn:
            etype = event.type

            # Audio chunks — the actual spoken response
            if etype == "response.output_audio.delta":
                audio_bytes = base64.b64decode(event.delta)
                yield S2SResponseEvent(type="audio", audio=audio_bytes)

            # Audio transcript (text of what the model is saying)
            elif etype == "response.output_audio_transcript.delta":
                session.output_transcript += event.delta
                yield S2SResponseEvent(type="transcript", text=event.delta)

            # Input transcript (what the user said — async via Whisper)
            elif etype == "conversation.item.input_audio_transcription.completed":
                if hasattr(event, "transcript") and event.transcript:
                    session.input_transcript += event.transcript

            # Response complete — check if it's a function call or final audio
            elif etype == "response.done":
                response = getattr(event, "response", None)
                output_items = getattr(response, "output", []) if response else []

                # Check for function calls in this response
                func_calls = [
                    item for item in output_items
                    if getattr(item, "type", None) == "function_call"
                ]

                if func_calls:
                    # Handle each function call
                    for fc in func_calls:
                        name = getattr(fc, "name", "")
                        args = getattr(fc, "arguments", "{}")
                        call_id = getattr(fc, "call_id", "")

                        yield S2SResponseEvent(
                            type="function_call",
                            function_name=name,
                            function_args=args,
                            call_id=call_id,
                        )

                        # Dispatch the tool call
                        result = await self._bridge.handle_tool_call(name, args)

                        # Send result back to model
                        await conn.conversation.item.create(item={
                            "type": "function_call_output",
                            "call_id": call_id,
                            "output": result,
                        })

                    # Trigger the follow-up audio response
                    await conn.response.create()
                    # Continue loop — next response will have audio
                else:
                    # No function calls — this is the final audio response
                    session.turn_count += 1
                    session.last_activity = datetime.now(UTC)
                    session.output_transcript += "\n"
                    yield S2SResponseEvent(type="done")
                    break

            # Error — log but only break on fatal errors
            elif etype == "error":
                error = getattr(event, "error", event)
                code = getattr(error, "code", "")
                msg = str(error)

                # Non-fatal: stale buffer commit from previous VAD interaction
                if code == "input_audio_buffer_commit_empty":
                    logger.debug("Ignoring empty buffer commit error")
                    continue

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
