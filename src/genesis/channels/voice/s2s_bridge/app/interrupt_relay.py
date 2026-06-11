"""Relay client-initiated interrupts into the pipeline."""
import logging

from pipecat.frames.frames import Frame, InputTransportMessageFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.openai.realtime import events

logger = logging.getLogger(__name__)


class InterruptRelay(FrameProcessor):
    """Converts firmware ``{"type": "interrupt"}`` messages into interruptions.

    The Voice PE firmware sends an interrupt text frame when the user barges
    in via the wake word during bot speech (it also stops its own speaker
    locally). This processor completes the server side:

    1. Cancels the in-flight OpenAI response. The service only does this
       itself when turn detection is disabled (``_handle_interruption``);
       with semantic VAD the server cancels on ``speech_started``, which
       never fires for client-side interrupts because the firmware mic gate
       blocked the wake word audio.
    2. Broadcasts an ``InterruptionFrame`` — truncates the OpenAI context,
       flushes queued output audio, and (via the output transport +
       ``RawAudioSerializer``) echoes the interrupt back to the device.
    """

    def __init__(self, openai_service, **kwargs):
        super().__init__(**kwargs)
        self._openai_service = openai_service

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, InputTransportMessageFrame) and self._is_interrupt(frame.message):
            logger.info("🖐️ Client interrupt received — cancelling response and broadcasting interruption")
            try:
                await self._openai_service.send_client_event(events.ResponseCancelEvent())
            except Exception as exc:
                logger.warning("Could not cancel OpenAI response: %s", exc)
            await self.broadcast_interruption()

        await self.push_frame(frame, direction)

    @staticmethod
    def _is_interrupt(message) -> bool:
        return isinstance(message, dict) and message.get("type") == "interrupt"
