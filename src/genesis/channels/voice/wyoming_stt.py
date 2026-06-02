"""Wyoming STT server — receives audio from HA, routes to S2S or fallback.

When the S2S model is available, audio is forwarded to the live session
and the model handles everything (conversation + tool calls + response).
The Wyoming protocol still requires a transcript response, which we
provide from the S2S model's input transcription.

When S2S is unavailable, falls back to Groq Whisper for transcription.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import wave
from io import BytesIO
from typing import TYPE_CHECKING

from wyoming.asr import Transcript
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.event import Event
from wyoming.info import (
    AsrModel,
    AsrProgram,
    Attribution,
    Describe,
    Info,
)
from wyoming.server import AsyncEventHandler, AsyncServer

from genesis.channels.voice import config as voice_config

if TYPE_CHECKING:
    from genesis.channels.voice.s2s_session import S2SSessionManager
    from genesis.channels.voice.wyoming_tts import WyomingTTSServer

logger = logging.getLogger(__name__)

_ATTRIBUTION = Attribution(name="Genesis", url="")


class STTEventHandler(AsyncEventHandler):
    """Handles a single Wyoming STT client connection from HA."""

    def __init__(
        self,
        *args,
        s2s_manager: S2SSessionManager | None = None,
        tts_server: WyomingTTSServer | None = None,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._s2s_manager = s2s_manager
        self._tts_server = tts_server
        self._audio_bytes = bytearray()
        self._rate = 16000
        self._width = 2
        self._channels = 1

    async def handle_event(self, event: Event) -> bool:
        if Describe.is_type(event.type):
            info = Info(
                asr=[AsrProgram(
                    name="genesis-stt",
                    attribution=_ATTRIBUTION,
                    installed=True,
                    description="Genesis STT (S2S or Groq Whisper fallback)",
                    version="2.0.0",
                    models=[AsrModel(
                        name="genesis-voice",
                        attribution=_ATTRIBUTION,
                        installed=True,
                        description="Genesis voice pipeline",
                        version="2.0.0",
                        languages=["en"],
                    )],
                )],
            )
            await self.write_event(info.event())
            return True

        if AudioStart.is_type(event.type):
            start = AudioStart.from_event(event)
            self._rate = start.rate
            self._width = start.width
            self._channels = start.channels
            self._audio_bytes.clear()
            return True

        if AudioChunk.is_type(event.type):
            chunk = AudioChunk.from_event(event)
            self._audio_bytes.extend(chunk.audio)
            return True

        if AudioStop.is_type(event.type):
            audio = bytes(self._audio_bytes)
            self._audio_bytes.clear()

            if not audio:
                await self.write_event(Transcript(text="").event())
                return True

            # Route: S2S model or fallback
            if (
                self._s2s_manager
                and voice_config.s2s_enabled()
            ):
                try:
                    transcript = await asyncio.wait_for(
                        self._handle_s2s(audio), timeout=60,
                    )
                except TimeoutError:
                    logger.error("S2S round-trip timed out (60s), falling back")
                    transcript = await self._handle_fallback(audio)
            else:
                transcript = await self._handle_fallback(audio)

            await self.write_event(Transcript(text=transcript).event())
            return True

        return True

    # GPT-Realtime requires >= 100ms of audio. At 16kHz 16-bit mono,
    # 100ms = 3200 bytes. Use 150ms (4800 bytes) as safety margin.
    _MIN_AUDIO_BYTES = 4800

    async def _handle_s2s(self, audio: bytes) -> str:
        """Forward audio to S2S model, play response via TTS server.

        60s timeout on the entire S2S round-trip. Justified: a voice
        response should never take >60s including tool calls. If it does,
        the connection is dead and we fall back to Groq Whisper.
        """
        # Reject clips too short for the Realtime API (< 150ms)
        if len(audio) < self._MIN_AUDIO_BYTES:
            duration_ms = len(audio) / (self._rate * self._width) * 1000
            logger.warning(
                "Audio too short for S2S (%.0fms < 150ms), falling back",
                duration_ms,
            )
            return await self._handle_fallback(audio)

        # Derive satellite ID from peer address — supports multiple satellites
        satellite_id = "ha-voice-default"
        if hasattr(self, "client_id") and self.client_id:
            satellite_id = f"ha-{self.client_id}"

        try:
            session = await self._s2s_manager.get_or_create(satellite_id)
            if session.connection is None:
                await self._s2s_manager.connect(session)

            # Send audio to model (resampled 16kHz → 24kHz internally)
            await self._s2s_manager.send_audio(session, audio, input_rate=self._rate)
            await self._s2s_manager.commit_audio(session)

            # Collect full response — audio AND transcript — before returning.
            # HA's pipeline is sequential: STT → conversation agent → TTS.
            # The audio MUST be queued on the TTS server BEFORE we return
            # the transcript, otherwise HA fires TTS with an empty queue.
            response_audio = bytearray()
            transcript = ""

            async for event in self._s2s_manager.receive_response(session):
                if event.type == "audio" and event.audio:
                    response_audio.extend(event.audio)
                elif event.type == "transcript":
                    transcript += event.text
                elif event.type == "function_call":
                    logger.info(
                        "S2S tool call: %s(%s)",
                        event.function_name,
                        event.function_args[:100],
                    )
                elif event.type == "done":
                    break
                elif event.type == "error":
                    logger.error("S2S response error: %s", event.text)
                    return await self._handle_fallback(audio)

            # Queue response audio for TTS server BEFORE returning transcript
            if response_audio and self._tts_server:
                self._tts_server.queue_audio(bytes(response_audio))
                logger.info(
                    "S2S audio queued: %d bytes (%.1fs at 24kHz)",
                    len(response_audio),
                    len(response_audio) / (24000 * 2),
                )
            elif not response_audio:
                logger.warning("S2S response had no audio data")

            return transcript or "(no transcription)"

        except Exception:
            logger.exception("S2S processing failed, falling back")
            return await self._handle_fallback(audio)

    async def _handle_fallback(self, audio: bytes) -> str:
        """Fallback: transcribe via Groq Whisper."""
        try:
            # Convert raw PCM to WAV for Groq
            wav_bytes = self._pcm_to_wav(audio)
            from genesis.channels.stt import transcribe
            return await transcribe(wav_bytes)
        except Exception:
            logger.exception("Fallback STT failed")
            return ""

    def _pcm_to_wav(self, pcm: bytes) -> bytes:
        """Wrap raw PCM in a WAV header."""
        buf = BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(self._channels)
            w.setsampwidth(self._width)
            w.setframerate(self._rate)
            w.writeframes(pcm)
        return buf.getvalue()


class WyomingSTTServer:
    """Wyoming STT server that integrates with the S2S pipeline."""

    def __init__(
        self,
        *,
        s2s_manager: S2SSessionManager | None = None,
        tts_server: WyomingTTSServer | None = None,
        host: str = "0.0.0.0",
        port: int | None = None,
    ) -> None:
        self._s2s_manager = s2s_manager
        self._tts_server = tts_server
        self._host = host
        self._port = port or voice_config.wyoming_stt_port()
        self._server: AsyncServer | None = None
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        """Start the Wyoming STT server."""
        uri = f"tcp://{self._host}:{self._port}"
        self._server = AsyncServer.from_uri(uri)

        from functools import partial
        handler_factory = partial(
            STTEventHandler,
            s2s_manager=self._s2s_manager,
            tts_server=self._tts_server,
        )

        self._task = asyncio.create_task(
            self._server.run(handler_factory),
            name="wyoming-stt",
        )
        logger.info("Wyoming STT server started on %s", uri)

    async def stop(self) -> None:
        """Stop the server."""
        if self._task and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        logger.info("Wyoming STT server stopped")
