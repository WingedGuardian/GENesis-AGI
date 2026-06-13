"""Simple serializer for raw binary PCM audio frames."""
import json
import logging

from pipecat.frames.frames import (
    Frame,
    InputAudioRawFrame,
    InputTransportMessageFrame,
    InterruptionFrame,
    OutputAudioRawFrame,
)
from pipecat.serializers.base_serializer import FrameSerializer

logger = logging.getLogger(__name__)


class RawAudioSerializer(FrameSerializer):
    """Serializer for the Voice PE protocol.

    Binary WebSocket frames carry raw PCM audio (16-bit, 24kHz, mono).
    Text frames carry JSON control messages (e.g. ``{"type": "interrupt"}``).
    """

    async def deserialize(self, message: bytes | str) -> Frame | None:
        """Deserialize a WebSocket message into a pipeline frame.

        Args:
            message: Binary PCM audio data (16-bit, 24kHz, mono) or a JSON
                text control message from the firmware.

        Returns:
            InputAudioRawFrame for audio, InputTransportMessageFrame for
            control messages, or None if invalid.
        """
        if isinstance(message, str):
            # Text frames are JSON control messages from the firmware
            # (e.g. wake-word barge-in sends {"type": "interrupt"}).
            try:
                data = json.loads(message)
            except ValueError:
                logger.warning(f"⚠️ Ignoring non-JSON text message: {message[:100]}")
                return None
            logger.info(f"📥 Control message from client: {data}")
            return InputTransportMessageFrame(message=data)

        if not isinstance(message, bytes):
            return None

        # Validate audio format: 16-bit = 2 bytes per sample
        if len(message) % 2 != 0:
            logger.warning(f"⚠️ Received audio with odd byte count: {len(message)} bytes, skipping")
            return None

        # Create InputAudioRawFrame
        # Audio is 24kHz, 16-bit, mono PCM
        frame = InputAudioRawFrame(
            audio=message,
            sample_rate=24000,
            num_channels=1
        )

        return frame

    async def serialize(self, frame: Frame) -> bytes | str:
        """Serialize a frame to a WebSocket message.

        Output audio frames become binary messages (raw PCM bytes).
        InterruptionFrame becomes a JSON text message — the firmware stops
        its speaker and clears buffered audio on receipt (a str return is
        sent as a WebSocket text frame by the websockets library).
        Other frames are not serialized (empty bytes are skipped by the
        transport's send guard).
        """
        if isinstance(frame, OutputAudioRawFrame):
            audio_bytes = frame.audio
            logger.debug(f"📤 Serializing OutputAudioRawFrame: {len(audio_bytes)} bytes")
            return audio_bytes
        if isinstance(frame, InterruptionFrame):
            # The websocket output transport forwards InterruptionFrame here
            # (pipecat server.py process_frame). Tell the device to stop
            # playback immediately instead of draining its local buffer.
            logger.info("📤 Sending interrupt control message to client")
            return '{"type":"interrupt"}'
        # For other frame types, return empty bytes (not serialized)
        logger.debug(f"📤 Serializing non-audio frame: {type(frame).__name__}, returning empty bytes")
        return b""

