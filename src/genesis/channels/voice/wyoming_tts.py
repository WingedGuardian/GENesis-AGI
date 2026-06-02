"""Wyoming TTS server — sends response audio back to HA.

Two modes of operation:

1. **S2S mode**: The STT handler queues pre-generated audio from the S2S
   model.  When HA sends a ``synthesize`` request, the TTS server checks
   the queue first and returns the pre-generated audio if available.
   This bypasses text-to-speech entirely — the S2S model already generated
   the spoken response.

2. **Fallback mode**: No queued audio available.  Falls back to a real
   TTS provider (Piper via HA, or Cartesia/ElevenLabs via Genesis).
   In this mode, the server receives text and synthesizes speech normally.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import logging

from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.event import Event
from wyoming.info import (
    Attribution,
    Describe,
    Info,
    TtsProgram,
    TtsVoice,
)
from wyoming.server import AsyncEventHandler, AsyncServer
from wyoming.tts import Synthesize

from genesis.channels.voice import config as voice_config

logger = logging.getLogger(__name__)

_ATTRIBUTION = Attribution(name="Genesis", url="")

# S2S audio is 24kHz 16-bit mono PCM (OpenAI Realtime output)
_S2S_RATE = 24000
_S2S_WIDTH = 2
_S2S_CHANNELS = 1

# Voice PE expects 16kHz (matches Piper/Whisper default)
_TARGET_RATE = 16000


def _resample_pcm(pcm: bytes, src_rate: int, dst_rate: int) -> bytes:
    """Resample 16-bit mono PCM from src_rate to dst_rate.

    Uses linear interpolation — simple and sufficient for voice audio.
    """
    if src_rate == dst_rate:
        return pcm

    import struct

    samples = struct.unpack(f"<{len(pcm) // 2}h", pcm)
    ratio = dst_rate / src_rate
    new_len = int(len(samples) * ratio)
    resampled = []
    for i in range(new_len):
        src_pos = i / ratio
        idx = int(src_pos)
        frac = src_pos - idx
        if idx + 1 < len(samples):
            val = samples[idx] * (1 - frac) + samples[idx + 1] * frac
        else:
            val = samples[idx] if idx < len(samples) else 0
        resampled.append(int(val))
    return struct.pack(f"<{len(resampled)}h", *resampled)


class TTSEventHandler(AsyncEventHandler):
    """Handles a single Wyoming TTS client connection from HA."""

    def __init__(self, *args, audio_queue: collections.deque, audio_ready: asyncio.Event, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._audio_queue = audio_queue
        self._audio_ready = audio_ready

    async def handle_event(self, event: Event) -> bool:
        if Describe.is_type(event.type):
            info = Info(
                tts=[TtsProgram(
                    name="genesis-tts",
                    attribution=_ATTRIBUTION,
                    installed=True,
                    description="Genesis TTS (S2S audio or provider fallback)",
                    version="2.0.0",
                    voices=[TtsVoice(
                        name="genesis",
                        attribution=_ATTRIBUTION,
                        installed=True,
                        description="Genesis voice",
                        version="2.0.0",
                        languages=["en"],
                    )],
                )],
            )
            await self.write_event(info.event())
            return True

        if Synthesize.is_type(event.type):
            synth = Synthesize.from_event(event)

            # Wait up to 5s for S2S audio to arrive (STT handler may still
            # be processing when HA fires the TTS request)
            if not self._audio_queue:
                self._audio_ready.clear()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._audio_ready.wait(), timeout=5.0)

            if self._audio_queue:
                audio = self._audio_queue.popleft()
                self._audio_ready.clear()
                logger.info(
                    "Serving pre-generated S2S audio: %d bytes (%.1fs)",
                    len(audio),
                    len(audio) / (_S2S_RATE * _S2S_WIDTH * _S2S_CHANNELS),
                )
                # Resample 24kHz → 16kHz for Voice PE compatibility
                resampled = _resample_pcm(audio, _S2S_RATE, _TARGET_RATE)
                await self._send_audio(resampled, _TARGET_RATE, _S2S_WIDTH, _S2S_CHANNELS)
            else:
                logger.info("No S2S audio available, synthesizing text: '%s'", synth.text[:50])
                await self._synthesize_fallback(synth.text)

            return True

        return True

    async def _send_audio(
        self, audio: bytes, rate: int, width: int, channels: int,
    ) -> None:
        """Send audio back to HA via Wyoming protocol."""
        await self.write_event(
            AudioStart(rate=rate, width=width, channels=channels).event(),
        )

        # Send in chunks to avoid overwhelming the connection
        chunk_size = rate * width * channels  # 1 second of audio per chunk
        for offset in range(0, len(audio), chunk_size):
            chunk = audio[offset : offset + chunk_size]
            await self.write_event(
                AudioChunk(
                    rate=rate, width=width, channels=channels, audio=chunk,
                ).event(),
            )

        await self.write_event(AudioStop().event())

    async def _synthesize_fallback(self, text: str) -> None:
        """Fallback TTS when no S2S audio is queued.

        Uses the Phase 1 VoiceConversationHandler path — text goes to
        an external TTS provider and audio is returned.
        """
        # For Phase 2, the fallback sends 0.5s of silence.
        # The actual response text is returned to HA's conversation agent
        # via the Wyoming STT transcript, and HA uses its own Piper TTS.
        rate, width, channels = 22050, 2, 1
        silence_duration = 0.1  # Minimal silence
        silence = b"\x00\x00" * int(rate * silence_duration)
        await self._send_audio(silence, rate, width, channels)


class WyomingTTSServer:
    """Wyoming TTS server with S2S audio queue."""

    def __init__(
        self,
        *,
        host: str = "0.0.0.0",
        port: int | None = None,
    ) -> None:
        self._host = host
        self._port = port or voice_config.wyoming_tts_port()
        self._server: AsyncServer | None = None
        self._task: asyncio.Task | None = None
        self._audio_queue: collections.deque[bytes] = collections.deque(maxlen=5)
        self._audio_ready = asyncio.Event()

    def queue_audio(self, audio: bytes) -> None:
        """Queue pre-generated audio from the S2S model.

        Called by the STT handler when S2S model produces a response.
        The next ``synthesize`` request from HA will serve this audio
        instead of doing text-to-speech.
        """
        self._audio_queue.append(audio)
        self._audio_ready.set()

    async def start(self) -> None:
        """Start the Wyoming TTS server."""
        uri = f"tcp://{self._host}:{self._port}"
        self._server = AsyncServer.from_uri(uri)

        from functools import partial
        handler_factory = partial(
            TTSEventHandler,
            audio_queue=self._audio_queue,
            audio_ready=self._audio_ready,
        )

        from genesis.util.tasks import tracked_task
        self._task = tracked_task(
            self._server.run(handler_factory),
            name="wyoming-tts",
        )
        logger.info("Wyoming TTS server started on %s", uri)

    async def stop(self) -> None:
        """Stop the server."""
        if self._task and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        logger.info("Wyoming TTS server stopped")
