"""Voice delivery helper — channel-agnostic synthesize+deliver pattern."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from genesis.channels.tts_config import TTSConfigLoader, sanitize_for_speech

if TYPE_CHECKING:
    from genesis.channels.base import ChannelAdapter
    from genesis.providers.tts import TTSProvider

logger = logging.getLogger(__name__)


class VoiceDeliveryHelper:
    """Stateless helper: sanitize text, synthesize audio, deliver via adapter.

    Encapsulates the synthesize-then-deliver pattern so any channel adapter
    (or future outreach pipeline) can send voice without Telegram-specific code.
    """

    def __init__(
        self,
        provider: TTSProvider,
        config_loader: TTSConfigLoader | None = None,
    ) -> None:
        self._provider = provider
        self._config_loader = config_loader

    @property
    def available(self) -> bool:
        return self._provider is not None

    async def synthesize_and_deliver(
        self,
        adapter: ChannelAdapter,
        channel_id: str,
        text: str,
        *,
        reply_to_message_id: str | None = None,
    ) -> bool:
        """Sanitize, synthesize, deliver voice. Returns True if voice was sent."""
        if not text:
            return False

        try:
            config = self._config_loader.load() if self._config_loader else None
        except Exception:
            logger.warning("TTS config load failed — using defaults", exc_info=True)
            config = None
        sanitized = sanitize_for_speech(
            text, config.sanitization if config else None
        )

        # Step 1: synthesize
        audio = None
        try:
            audio = await self._provider.synthesize(sanitized)
        except Exception:
            logger.error("TTS synthesis failed for %d chars", len(sanitized), exc_info=True)
            return False

        if not audio:
            logger.warning("TTS synthesis returned empty audio (%d chars input)", len(sanitized))
            return False

        # Step 2: deliver (adapter handles channel-specific timeout semantics)
        try:
            await adapter.send_voice(
                channel_id, audio, reply_to_message_id=reply_to_message_id
            )
            logger.info(
                "Voice delivered to %s (%d bytes audio)", channel_id, len(audio)
            )
            return True
        except Exception:
            logger.warning("Voice delivery failed", exc_info=True)
            return False
